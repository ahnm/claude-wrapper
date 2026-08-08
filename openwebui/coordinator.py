"""Architect Council coordinator — a tiny zero-dependency job queue.

Run next to claude-wrapper on the machine OpenWebUI can reach:

    python coordinator.py --port 8787 [--token SECRET] [--state council_state.json]

The pipe function POSTs feature jobs (chunk DAGs); worker.py instances on any
machine claim chunks, build them, and report back. Bind is 0.0.0.0 so workers
on the LAN (e.g. a MacBook) can reach it — set --token if the network is shared.

API (JSON bodies, optional `X-Auth-Token` header):
  POST /jobs                                  create job -> {job_id}
  GET  /jobs/<id>                             full job status
  GET  /workers                               roster of workers seen recently
  POST /jobs/<id>/retry                       re-queue failed chunks/integration
  POST /claim        {worker, tags}           claim next eligible task -> task | 204
  POST /jobs/<id>/chunks/<cid>/report         {status: done|failed, log, worker}
  POST /jobs/<id>/integration/report          {status: done|failed, log, worker}

Worker selection: jobs may set allowed_workers (names); chunks may set
assign_to (names) and required_tags — a worker only receives tasks it matches.

Continue-on-error: a failed chunk is auto-requeued (with its failure log fed to
the next attempt) until max_attempts; integration starts once every chunk is
terminal and merges only the successful branches.
"""

import argparse
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STALE_SECONDS = 2 * 60 * 60  # reclaim tasks a worker never reported on

state_lock = threading.Lock()
state = {"jobs": {}, "workers": {}, "sessions": {}}
state_path = "council_state.json"
auth_token = ""
artifact_root = ""   # set by --artifacts; empty means /artifacts is disabled


def _safe_segment(value: str, fallback: str) -> str:
    """One path segment, letters/digits/dash only — never '..', never a drive."""
    seg = re.sub(r"[^\w-]+", "-", str(value or "")).strip("-.")[:60]
    return seg or fallback


def write_artifacts(body: dict):
    """Write {filename: content} under <root>/<venture>/v<n>/.

    The pipe runs inside the OpenWebUI container, so it cannot put files
    anywhere the founder can open them; the coordinator is a host process, so
    exporting goes through here. Every path component is sanitized and the
    result is re-checked against the root before anything is written.
    """
    if not artifact_root:
        raise ValueError("artifact export is disabled (start with --artifacts DIR)")
    files = body.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty object of name -> content")

    venture = _safe_segment(body.get("name"), "venture")
    version = _safe_segment(f"v{body.get('version', 1)}", "v1")
    folder = os.path.join(artifact_root, venture, version)
    os.makedirs(folder, exist_ok=True)

    written = []
    for raw_name, content in files.items():
        name = _safe_segment(os.path.splitext(os.path.basename(str(raw_name)))[0], "file")
        ext = os.path.splitext(str(raw_name))[1].lower()
        ext = ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext or "") else ".md"
        path = os.path.join(folder, name + ext)
        if os.path.commonpath([os.path.abspath(path), artifact_root]) != artifact_root:
            raise ValueError(f"refusing to write outside the artifact root: {raw_name}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content, indent=2))
        written.append(os.path.basename(path))
    return sorted(written), folder


def save_state():
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_state():
    global state
    try:
        with open(state_path, encoding="utf-8") as f:
            state.update(json.load(f))
        state.setdefault("workers", {})
        state.setdefault("sessions", {})
    except FileNotFoundError:
        pass


def topo_order(chunks):
    """Dependency-respecting order of chunk ids (Kahn's algorithm)."""
    ids = [c["id"] for c in chunks]
    deps = {c["id"]: [d for d in c.get("depends_on", []) if d in ids] for c in chunks}
    order, ready = [], [i for i in ids if not deps[i]]
    while ready:
        n = ready.pop(0)
        order.append(n)
        for i in ids:
            if n in deps[i]:
                deps[i].remove(n)
                if not deps[i] and i not in order and i not in ready:
                    ready.append(i)
    return order + [i for i in ids if i not in order]  # cycles appended last


def terminal(task):
    return task["status"] in ("done", "failed")


def job_status(job):
    statuses = [c["status"] for c in job["chunks"]]
    integ = job["integration"]["status"]
    if job["build_mode"] == "worktree":
        if all(s in ("done", "failed") for s in statuses):
            if integ == "done":
                return "complete_with_failures" if "failed" in statuses else "complete"
            if integ == "failed":
                return "needs_attention"
            if any(s == "done" for s in statuses):
                return "integrating"
            return "needs_attention"  # everything failed; nothing to integrate
    elif all(s in ("done", "failed") for s in statuses):
        return "complete_with_failures" if "failed" in statuses else "complete"
    return "building"


def stale(task):
    return task["status"] == "claimed" and time.time() - (task.get("claimed_at") or 0) > STALE_SECONDS


def claimable(task):
    return task["status"] == "pending" or stale(task)


def eligible(entity, job, worker, tags):
    if worker in (entity.get("blocked_workers") or []):
        return False  # this worker lacked a tool for this task
    allowed = entity.get("assign_to") or job.get("allowed_workers") or []
    if allowed and worker not in allowed:
        return False
    return set(entity.get("required_tags") or []).issubset(set(tags))


def claim_task(worker, tags):
    for job_id, job in state["jobs"].items():
        chunks = job["chunks"]
        if job["build_mode"] == "worktree":
            for c in chunks:
                if claimable(c) and eligible(c, job, worker, tags):
                    c.update(status="claimed", worker=worker, claimed_at=time.time())
                    return build_task(job_id, job, c)
            integ = job["integration"]
            if (all(terminal(c) for c in chunks)
                    and any(c["status"] == "done" for c in chunks)
                    and claimable(integ)
                    and eligible(integ, job, worker, tags)):
                integ.update(status="claimed", worker=worker, claimed_at=time.time())
                return integrate_task(job_id, job)
        else:  # main mode: strictly one chunk at a time, dependency order
            if any(c["status"] == "claimed" and not stale(c) for c in chunks):
                continue
            done = {c["id"] for c in chunks if c["status"] == "done"}
            for cid in topo_order(chunks):
                c = next(x for x in chunks if x["id"] == cid)
                if claimable(c) and eligible(c, job, worker, tags) \
                        and all(d in done for d in c.get("depends_on", [])):
                    c.update(status="claimed", worker=worker, claimed_at=time.time())
                    return build_task(job_id, job, c)
    return None


def build_task(job_id, job, chunk):
    return {
        "task_type": "build",
        "job_id": job_id,
        "chunk_id": chunk["id"],
        "title": chunk["title"],
        "brief": chunk["brief"],
        "repo_url": job["repo_url"],
        "base_branch": job["base_branch"],
        "build_mode": job["build_mode"],
        "branch": chunk["branch"],
        "test_command": chunk.get("test_command") or "",
        "attempt": chunk.get("attempts", 0) + 1,
        "last_failure": chunk.get("log", "") if chunk.get("attempts", 0) else "",
    }


def integrate_task(job_id, job):
    done = {c["id"] for c in job["chunks"] if c["status"] == "done"}
    order = [i for i in topo_order(job["chunks"]) if i in done]
    branch_of = {c["id"]: c["branch"] for c in job["chunks"]}
    return {
        "task_type": "integrate",
        "job_id": job_id,
        "repo_url": job["repo_url"],
        "base_branch": job["base_branch"],
        "integration_branch": job["integration"]["branch"],
        "merge_order": [branch_of[i] for i in order],
        "excluded_chunks": sorted(c["id"] for c in job["chunks"] if c["status"] == "failed"),
        "test_command": job.get("test_command") or "",
        "e2e_command": job["integration"].get("e2e_command") or "",
    }


def create_job(body):
    job_id = uuid.uuid4().hex[:8]
    build_mode = body.get("build_mode", "worktree")
    chunks = []
    for c in body["chunks"]:
        branch = body["base_branch"] if build_mode == "main" else f"council/{job_id}/{c['id']}"
        chunks.append({
            "id": c["id"],
            "title": c.get("title", c["id"]),
            "brief": c["brief"],
            "depends_on": c.get("depends_on", []),
            "test_command": c.get("test_command", body.get("test_command", "")),
            "assign_to": c.get("assign_to", []),
            "required_tags": c.get("required_tags", []),
            "status": "pending", "worker": None, "claimed_at": None,
            "branch": branch, "log": "", "attempts": 0, "blocked_workers": [],
        })
    state["jobs"][job_id] = {
        "name": body.get("name", job_id),
        "repo_url": body["repo_url"],
        "base_branch": body.get("base_branch", "main"),
        "build_mode": build_mode,
        "test_command": body.get("test_command", ""),
        "allowed_workers": body.get("allowed_workers", []),
        "max_attempts": int(body.get("max_attempts", 3)),
        "chunks": chunks,
        "integration": {
            "status": "pending" if build_mode == "worktree" else "done",
            "branch": f"council/{job_id}/integration",
            "e2e_command": body.get("e2e_command", ""),
            "assign_to": body.get("integrator_workers", []),
            "worker": None, "claimed_at": None, "log": "", "attempts": 0,
            "blocked_workers": [],
        },
        "created_at": time.time(),
    }
    return job_id


def record_failure(task, job, reason="", worker=None):
    """Continue-on-error: auto-requeue until max_attempts, then mark failed.
    Environment failures (missing tools) don't consume an attempt — the task
    is re-queued with that worker blocked so a capable machine picks it up."""
    if reason == "environment" and worker:
        blocked = task.setdefault("blocked_workers", [])
        if worker not in blocked:
            blocked.append(worker)
        task.update(status="pending", worker=None, claimed_at=None)
        return
    task["attempts"] = task.get("attempts", 0) + 1
    if task["attempts"] < job.get("max_attempts", 3):
        task.update(status="pending", worker=None, claimed_at=None)
    else:
        task["status"] = "failed"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter logs
        print(f"{self.address_string()} {fmt % args}")

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _authed(self):
        if auth_token and self.headers.get("X-Auth-Token") != auth_token:
            self._json(401, {"error": "bad token"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        m = re.fullmatch(r"/jobs/(\w+)", self.path)
        with state_lock:
            if m and m.group(1) in state["jobs"]:
                job = state["jobs"][m.group(1)]
                return self._json(200, {**job, "status": job_status(job)})
            if self.path == "/jobs":
                return self._json(200, {
                    j: {"name": job["name"], "status": job_status(job)}
                    for j, job in state["jobs"].items()
                })
            if self.path == "/workers":
                return self._json(200, {
                    name: {**w, "seen_seconds_ago": int(time.time() - w.get("last_seen", 0))}
                    for name, w in state["workers"].items()
                })
            if self.path == "/sessions":
                return self._json(200, {
                    sid: {"name": s.get("name", ""), "phase": s.get("phase", ""),
                          "job_id": s.get("job_id"),
                          "updated_seconds_ago": int(time.time() - s.get("updated_at", 0))}
                    for sid, s in state["sessions"].items()
                })
            m = re.fullmatch(r"/sessions/(\w+)", self.path)
            if m and m.group(1) in state["sessions"]:
                return self._json(200, state["sessions"][m.group(1)])
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        try:
            body = self._body()
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})

        with state_lock:
            if self.path == "/jobs":
                job_id = create_job(body)
                save_state()
                return self._json(201, {"job_id": job_id})

            if self.path == "/sessions":
                sid = body.pop("session_id", None)
                if not sid:
                    return self._json(400, {"error": "session_id required"})
                # merge-upsert: partial checkpoints must not erase earlier fields
                sess = state["sessions"].setdefault(sid, {})
                sess.update(body)
                sess["updated_at"] = time.time()
                save_state()
                return self._json(200, {"ok": True})

            if self.path == "/artifacts":
                try:
                    written, folder = write_artifacts(body)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                except OSError as e:
                    return self._json(500, {"error": f"write failed: {e}"})
                return self._json(200, {"folder": folder, "written": written})

            if self.path == "/claim":
                worker = body.get("worker", "anonymous")
                tags = body.get("tags", [])
                state["workers"][worker] = {
                    "tags": tags,
                    "platform": body.get("platform", ""),
                    "last_seen": time.time(),
                }
                task = claim_task(worker, tags)
                save_state()
                if task:
                    return self._json(200, task)
                return self._json(204, {})

            m = re.fullmatch(r"/jobs/(\w+)/retry", self.path)
            if m and m.group(1) in state["jobs"]:
                job = state["jobs"][m.group(1)]
                for c in job["chunks"]:
                    if c["status"] == "failed":
                        c.update(status="pending", worker=None, claimed_at=None, attempts=0)
                if job["integration"]["status"] == "failed":
                    job["integration"].update(status="pending", worker=None,
                                              claimed_at=None, attempts=0)
                save_state()
                return self._json(200, {"ok": True})

            m = re.fullmatch(r"/jobs/(\w+)/chunks/([\w-]+)/report", self.path)
            if m and m.group(1) in state["jobs"]:
                job = state["jobs"][m.group(1)]
                for c in job["chunks"]:
                    if c["id"] == m.group(2):
                        c["log"] = body.get("log", "")[-8000:]
                        c["worker"] = body.get("worker", c.get("worker"))
                        if body.get("status") == "done":
                            c["status"] = "done"
                        else:
                            record_failure(c, job, body.get("reason", ""),
                                           body.get("worker"))
                        save_state()
                        return self._json(200, {"ok": True})

            m = re.fullmatch(r"/jobs/(\w+)/integration/report", self.path)
            if m and m.group(1) in state["jobs"]:
                job = state["jobs"][m.group(1)]
                integ = job["integration"]
                integ["log"] = body.get("log", "")[-8000:]
                integ["worker"] = body.get("worker")
                if body.get("status") == "done":
                    integ["status"] = "done"
                else:
                    record_failure(integ, job, body.get("reason", ""),
                                   body.get("worker"))
                save_state()
                return self._json(200, {"ok": True})

        self._json(404, {"error": "not found"})


def main():
    global state_path, auth_token
    ap = argparse.ArgumentParser(description="Architect Council coordinator")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default="", help="shared secret for pipe + workers")
    ap.add_argument("--state", default="council_state.json")
    ap.add_argument("--artifacts", default="",
                    help="directory for plan exports; empty disables /artifacts")
    args = ap.parse_args()
    state_path, auth_token = args.state, args.token
    global artifact_root
    artifact_root = os.path.abspath(args.artifacts) if args.artifacts else ""
    load_state()
    print(f"Coordinator listening on 0.0.0.0:{args.port} (state: {state_path})")
    print(f"Artifacts: {artifact_root or 'disabled (pass --artifacts DIR)'}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
