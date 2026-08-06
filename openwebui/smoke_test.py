"""Smoke test for coordinator.py scheduling: eligibility, auto-retry,
continue-on-error integration gating. Run: python smoke_test.py"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 8790
BASE = f"http://127.0.0.1:{PORT}"


def http(method, path, payload=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status == 204:
            return None
        return json.loads(r.read() or b"{}")


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


state_file = Path(tempfile.mkdtemp()) / "state.json"
proc = subprocess.Popen(
    [sys.executable, "coordinator.py", "--port", str(PORT), "--state", str(state_file)],
    cwd=Path(__file__).parent,
)
try:
    for _ in range(50):
        try:
            http("GET", "/jobs")
            break
        except Exception:
            time.sleep(0.1)

    job = http("POST", "/jobs", {
        "name": "smoke", "repo_url": "https://example.com/repo.git",
        "base_branch": "main", "build_mode": "worktree",
        "max_attempts": 2, "e2e_command": "e2e", "test_command": "test",
        "integrator_workers": ["windows-box"],
        "chunks": [
            {"id": "api", "title": "API", "brief": "b", "assign_to": ["macbook-air"]},
            {"id": "ui", "title": "UI", "brief": "b", "depends_on": ["api"],
             "required_tags": ["gpu"]},
        ],
    })["job_id"]

    # eligibility: windows-box (no gpu tag) can't take either chunk
    t = http("POST", "/claim", {"worker": "windows-box", "tags": ["windows"]})
    check("pinned chunks refused to wrong worker", t is None)

    # macbook-air takes its assigned chunk; gpu tag lets it take 'ui' too
    t = http("POST", "/claim", {"worker": "macbook-air", "tags": ["darwin", "gpu"]})
    check("assign_to honored", t and t["chunk_id"] == "api")
    t2 = http("POST", "/claim", {"worker": "macbook-air", "tags": ["darwin", "gpu"]})
    check("required_tags honored (parallel worktree claim)", t2 and t2["chunk_id"] == "ui")

    # auto-retry: first failure re-queues with the log attached
    http("POST", f"/jobs/{job}/chunks/ui/report",
         {"status": "failed", "log": "boom", "worker": "macbook-air"})
    t3 = http("POST", "/claim", {"worker": "macbook-air", "tags": ["gpu"]})
    check("failed chunk auto-requeued", t3 and t3["chunk_id"] == "ui")
    check("failure log fed to retry", t3["last_failure"] == "boom" and t3["attempt"] == 2)

    # environment failure: no attempt consumed, worker blocked, others can claim
    http("POST", f"/jobs/{job}/chunks/ui/report",
         {"status": "failed", "reason": "environment",
          "log": "missing tool: npm", "worker": "macbook-air"})
    t_env = http("POST", "/claim", {"worker": "macbook-air", "tags": ["gpu"]})
    check("blocked worker cannot reclaim after missing tool", t_env is None)
    t_env2 = http("POST", "/claim", {"worker": "linux-box", "tags": ["gpu", "npm"]})
    check("capable worker picks up blocked chunk, attempt not consumed",
          t_env2 and t_env2["chunk_id"] == "ui" and t_env2["attempt"] == 2)

    # second real failure exhausts max_attempts=2 -> terminal failed
    http("POST", f"/jobs/{job}/chunks/ui/report",
         {"status": "failed", "log": "boom2", "worker": "linux-box"})
    http("POST", f"/jobs/{job}/chunks/api/report",
         {"status": "done", "log": "ok", "worker": "macbook-air"})

    # integration: gated to windows-box, merges only the successful chunk
    t4 = http("POST", "/claim", {"worker": "macbook-air", "tags": ["gpu"]})
    check("integration refused to non-integrator", t4 is None)
    t5 = http("POST", "/claim", {"worker": "windows-box", "tags": ["windows"]})
    check("integrator gets integrate task", t5 and t5["task_type"] == "integrate")
    check("failed chunk excluded from merge order",
          t5["merge_order"] == [f"council/{job}/api"] and t5["excluded_chunks"] == ["ui"])

    http("POST", f"/jobs/{job}/integration/report",
         {"status": "done", "log": "merged", "worker": "windows-box"})
    final = http("GET", f"/jobs/{job}")
    check("job completes with failures noted", final["status"] == "complete_with_failures")

    roster = http("GET", "/workers")
    check("worker roster tracked",
          set(roster) == {"windows-box", "macbook-air", "linux-box"})

    # session checkpoints: merge-upsert so partial saves don't erase fields
    http("POST", "/sessions", {"session_id": "abc123", "phase": "plan_review",
                               "name": "My Feature", "spec": "S", "design": "D"})
    http("POST", "/sessions", {"session_id": "abc123", "phase": "building",
                               "job_id": job})
    sess = http("GET", "/sessions/abc123")
    check("checkpoint merge-upsert keeps earlier fields",
          sess["spec"] == "S" and sess["phase"] == "building" and sess["job_id"] == job)
    listing = http("GET", "/sessions")
    check("session listing", listing["abc123"]["name"] == "My Feature"
          and listing["abc123"]["job_id"] == job)

    print("\nALL CHECKS PASSED")
finally:
    proc.terminate()
