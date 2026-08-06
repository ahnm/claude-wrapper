#!/usr/bin/env python3
"""Architect Council worker agent — zero dependencies (Python 3.9+, git).

Run on any machine that should build chunks (Windows box, MacBook, Codespace):

    python worker.py --coordinator http://<coordinator-host>:8787 \
        --name macbook-air --workdir ~/council --backend claude-code \
        [--tags gpu,online] [--token SECRET] [--once]

Backends:
  claude-code  runs the Claude Code CLI in the checkout (best quality; the
               machine needs `claude` installed and authenticated)
  wrapper      HTTP to a claude-wrapper /v1/chat/completions (needs --backend-url)
  ollama       HTTP to Ollama's OpenAI endpoint (needs --backend-url and --model)

The worker claims tasks it is eligible for (its --name and --tags are matched
against each chunk's assign_to / required_tags), builds them on a git worktree
branch or directly on the base branch depending on the job's build mode, runs
the chunk's test command, pushes, and reports back. A worker claiming an
`integrate` task merges the chunk branches in dependency order, testing after
each merge, then runs the end-to-end command.
"""

import argparse
import json
import platform
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

FILE_FORMAT_SYSTEM = """You are an autonomous implementation agent. You will
receive a chunk brief with full context. Implement it completely, including the
tests the brief asks for. For EVERY file you create or fully rewrite, output
exactly this format and nothing else:

===FILE: relative/path/to/file===
```
<complete file content>
```

Do not output explanations, partial files, or diffs."""


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

class ToolMissingError(RuntimeError):
    """A required command/tool is not installed on this machine.
    Reported as an 'environment' failure: the coordinator re-queues the task
    for other workers instead of burning a retry attempt here."""


KNOWN_TOOLS = ["git", "claude", "node", "npm", "npx", "python", "python3",
               "pytest", "pip", "docker", "go", "cargo", "dotnet", "make"]

MISSING_CMD_PATTERNS = (
    "is not recognized as an internal or external command",  # cmd.exe
    "command not found",                                     # sh/bash/zsh
)


def detect_tools():
    return sorted(t for t in KNOWN_TOOLS if shutil.which(t))


class Log:
    def __init__(self):
        self.lines = []

    def add(self, msg):
        print(msg, flush=True)
        self.lines.append(str(msg))

    def text(self):
        return "\n".join(self.lines)[-8000:]


def http(method, url, payload=None, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    req = urllib.request.Request(
        url, method=method, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status == 204:
            return None
        return json.loads(r.read() or b"{}")


def run(cmd, cwd, log, check=True, shell=False, timeout=7200):
    log.add(f"$ {cmd if shell else ' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           shell=shell, timeout=timeout)
    except FileNotFoundError:
        raise ToolMissingError(f"missing tool: {cmd if shell else cmd[0]}")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if out:
        log.add(out[-4000:])
    if check and p.returncode != 0:
        # 127 = sh "command not found", 9009 = cmd.exe "not recognized"
        if p.returncode in (127, 9009) or any(pat in out for pat in MISSING_CMD_PATTERNS):
            raise ToolMissingError(f"missing tool running: {cmd}")
        raise RuntimeError(f"command failed ({p.returncode})")
    return p


def git(repo, log, *args, check=True):
    return run(["git", "-C", str(repo), *args], repo.parent, log, check=check)


def sanitize(name):
    return re.sub(r"[^\w.-]", "_", name)


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

def backend_build(args, prompt, cwd, log):
    if args.backend == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            raise RuntimeError("claude CLI not found on PATH")
        perm = ["--dangerously-skip-permissions"] if args.yolo else ["--permission-mode", "acceptEdits"]
        run([claude, "-p", prompt, *perm], cwd, log)
        return

    if not args.backend_url:
        raise RuntimeError("--backend-url is required for wrapper/ollama backends")
    headers = {"Content-Type": "application/json"}
    if args.backend_key:
        headers["Authorization"] = f"Bearer {args.backend_key}"
    req = urllib.request.Request(
        f"{args.backend_url.rstrip('/')}/chat/completions",
        headers=headers,
        data=json.dumps({
            "model": args.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": FILE_FORMAT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }).encode(),
    )
    with urllib.request.urlopen(req, timeout=3600) as r:
        text = json.loads(r.read())["choices"][0]["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    files = re.findall(r"===FILE:\s*(.+?)\s*===\s*```[^\n]*\n(.*?)```", text, re.DOTALL)
    if not files:
        raise RuntimeError("model output contained no ===FILE:=== blocks")
    for path, content in files:
        target = Path(cwd) / path.strip()
        if ".." in target.parts:
            raise RuntimeError(f"refusing path traversal: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        log.add(f"wrote {path.strip()} ({len(content)} chars)")


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def ensure_repo(args, repo_url, log):
    repo = Path(args.workdir).expanduser() / "repo"
    repo.parent.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        run(["git", "clone", repo_url, str(repo)], repo.parent, log)
    else:
        git(repo, log, "fetch", "--all", "--prune")
    return repo


def commit_and_push(repo_or_wt, branch, message, args, log):
    git(repo_or_wt, log, "add", "-A")
    changed = git(repo_or_wt, log, "status", "--porcelain", check=False).stdout.strip()
    if not changed:
        raise RuntimeError("backend produced no changes")
    git(repo_or_wt, log,
        "-c", f"user.name=council-{args.name}",
        "-c", "user.email=council@localhost",
        "commit", "-m", message)
    git(repo_or_wt, log, "push", "-u", "origin", branch)


def do_build(args, task, log):
    repo = ensure_repo(args, task["repo_url"], log)
    base, branch = task["base_branch"], task["branch"]
    prompt = (
        "You are working inside a checkout of the repository. Implement the "
        "following chunk completely, including its tests.\n\n" + task["brief"]
    )
    if task.get("last_failure"):
        prompt += (
            f"\n\n--- PREVIOUS ATTEMPT (#{task.get('attempt', 2) - 1}) FAILED ---\n"
            "Diagnose the failure below and make sure this attempt fixes it:\n"
            + task["last_failure"][-3000:]
        )

    if task["build_mode"] == "worktree":
        wt = repo.parent / f"wt_{sanitize(branch)}"
        git(repo, log, "worktree", "remove", "--force", str(wt), check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        git(repo, log, "worktree", "add", "-B", branch, str(wt), f"origin/{base}")
        build_dir = wt
    else:  # main mode: serialized commits directly on the base branch
        git(repo, log, "checkout", base)
        git(repo, log, "pull", "--ff-only", "origin", base)
        build_dir = repo

    tests_ok, test_note = True, ""
    try:
        backend_build(args, prompt, build_dir, log)
        if task.get("test_command"):
            try:
                run(task["test_command"], build_dir, log, shell=True)
            except ToolMissingError:
                raise
            except Exception as e:
                tests_ok, test_note = False, f"chunk tests failed: {e}"
        commit_and_push(build_dir, branch, f"council: {task['title']} [{task['chunk_id']}]", args, log)
    finally:
        if task["build_mode"] == "worktree":
            git(repo, log, "worktree", "remove", "--force", str(build_dir), check=False)

    if not tests_ok:
        log.add(test_note)
        return "failed"
    return "done"


def try_repair(args, what, repo, log):
    """Ask the claude-code backend to fix the working tree; other backends can't."""
    if args.backend != "claude-code":
        return False
    log.add(f"attempting automated repair: {what}")
    try:
        backend_build(args, what, repo, log)
        return True
    except Exception as e:
        log.add(f"repair attempt errored: {e}")
        return False


def run_tests(args, cmd, repo, log, context):
    """Run tests; on failure attempt one automated repair, then rerun.
    Returns True if green (possibly after repair)."""
    try:
        run(cmd, repo, log, shell=True)
        return True
    except ToolMissingError:
        raise
    except Exception:
        pass
    if try_repair(
        args,
        f"The command `{cmd}` is failing in this repository ({context}). "
        "Run it, diagnose the failures, and fix the code (or the tests if they "
        "are genuinely wrong) until it passes.",
        repo,
        log,
    ):
        try:
            run(cmd, repo, log, shell=True)
            git(repo, log, "add", "-A")
            git(repo, log, "-c", f"user.name=council-{args.name}",
                "-c", "user.email=council@localhost",
                "commit", "-m", f"council: repair after {context}", check=False)
            return True
        except ToolMissingError:
            raise
        except Exception:
            pass
    return False


def do_integrate(args, task, log):
    repo = ensure_repo(args, task["repo_url"], log)
    base, integ = task["base_branch"], task["integration_branch"]
    git(repo, log, "checkout", "-B", integ, f"origin/{base}")
    excluded = list(task.get("excluded_chunks", []))
    if excluded:
        log.add(f"note: chunks excluded before integration (build failed): {excluded}")

    for branch in task["merge_order"]:
        log.add(f"--- merging {branch} ---")
        pre_merge = git(repo, log, "rev-parse", "HEAD").stdout.strip()
        merged = git(repo, log, "merge", "--no-ff", "--no-edit", f"origin/{branch}", check=False)

        if merged.returncode != 0:
            resolved = False
            if try_repair(
                args,
                "This repository has unresolved git merge conflicts. Resolve every "
                "conflict correctly (keep the intent of BOTH sides), remove all "
                "conflict markers, and stage the resolved files with `git add`. "
                "Do not commit.",
                repo,
                log,
            ):
                git(repo, log, "add", "-A")
                done = git(repo, log, "-c", f"user.name=council-{args.name}",
                           "-c", "user.email=council@localhost",
                           "commit", "--no-edit", check=False)
                resolved = done.returncode == 0
            if not resolved:
                # continue-on-error: drop this branch, keep integrating the rest
                git(repo, log, "merge", "--abort", check=False)
                git(repo, log, "reset", "--hard", pre_merge)
                excluded.append(branch)
                log.add(f"EXCLUDED {branch}: unresolvable merge conflict")
                continue

        # incremental verification: suite must stay green after every merge
        if task.get("test_command"):
            if not run_tests(args, task["test_command"], repo, log, f"merge of {branch}"):
                git(repo, log, "reset", "--hard", pre_merge)
                excluded.append(branch)
                log.add(f"EXCLUDED {branch}: tests stayed red after merge + repair attempt")
                continue

    e2e_ok = True
    if task.get("e2e_command"):
        log.add("--- end-to-end tests ---")
        e2e_ok = run_tests(args, task["e2e_command"], repo, log, "end-to-end run")

    git(repo, log, "push", "-u", "origin", integ)
    if excluded:
        log.add(f"integration finished WITH EXCLUSIONS: {excluded}")
    if not e2e_ok:
        log.add("e2e tests failed (integration branch pushed for inspection)")
        return "failed"
    return "done"


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def report(args, task, status, log, reason=""):
    job = task["job_id"]
    if task["task_type"] == "build":
        path = f"/jobs/{job}/chunks/{task['chunk_id']}/report"
    else:
        path = f"/jobs/{job}/integration/report"
    http("POST", args.coordinator.rstrip("/") + path,
         {"status": status, "log": log.text(), "worker": args.name, "reason": reason},
         args.token)


def preflight(args):
    """Verify this machine can actually do the work before claiming any."""
    problems = []
    if not shutil.which("git"):
        problems.append("git is not installed or not on PATH")
    if args.backend == "claude-code" and not shutil.which("claude"):
        problems.append("backend is claude-code but the `claude` CLI is not on PATH")
    if args.backend in ("wrapper", "ollama"):
        if not args.backend_url:
            problems.append(f"backend '{args.backend}' requires --backend-url")
        else:
            try:
                http("GET", args.backend_url.rstrip("/") + "/models",
                     token="")  # OpenAI-compatible liveness check
            except Exception as e:
                print(f"WARNING: backend {args.backend_url} not reachable yet ({e}); "
                      "continuing — tasks will fail as environment errors until it is up")
    if problems:
        for p in problems:
            print(f"PREFLIGHT FAILED: {p}")
        raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser(description="Architect Council worker agent")
    ap.add_argument("--coordinator", required=True)
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--tags", default="", help="comma-separated capabilities, e.g. gpu,online")
    ap.add_argument("--workdir", default="~/council")
    ap.add_argument("--backend", choices=["claude-code", "wrapper", "ollama"], default="claude-code")
    ap.add_argument("--backend-url", default="", help="OpenAI-compatible base URL for wrapper/ollama")
    ap.add_argument("--backend-key", default="")
    ap.add_argument("--model", default="qwen2.5-coder:32b")
    ap.add_argument("--token", default="", help="coordinator auth token")
    ap.add_argument("--poll", type=int, default=15, help="seconds between claim attempts")
    ap.add_argument("--once", action="store_true", help="process one task then exit")
    ap.add_argument("--yolo", action="store_true",
                    help="claude-code backend: --dangerously-skip-permissions (dedicated build machines only)")
    args = ap.parse_args()

    preflight(args)
    tools = detect_tools()
    tags = sorted({t.strip() for t in args.tags.split(",") if t.strip()}
                  | {platform.system().lower(), args.backend} | set(tools))
    print(f"worker '{args.name}' tags={tags} backend={args.backend} → {args.coordinator}")
    print(f"detected tools: {', '.join(tools) or 'none'}")

    while True:
        try:
            task = http("POST", args.coordinator.rstrip("/") + "/claim",
                        {"worker": args.name, "tags": tags,
                         "platform": platform.system().lower()}, args.token)
        except Exception as e:
            print(f"claim failed: {e}; retrying in {args.poll}s")
            task = None

        if not task:
            if args.once:
                return
            time.sleep(args.poll)
            continue

        log = Log()
        label = task.get("chunk_id") or "integration"
        log.add(f"=== claimed {task['task_type']} '{label}' (job {task['job_id']}) ===")
        reason = ""
        try:
            if task["task_type"] == "build":
                status = do_build(args, task, log)
            else:
                status = do_integrate(args, task, log)
        except ToolMissingError as e:
            log.add(f"ENVIRONMENT ERROR: {e} — task will be re-queued for other workers")
            status, reason = "failed", "environment"
        except Exception as e:
            log.add(f"ERROR: {e}")
            status = "failed"
        try:
            report(args, task, status, log, reason)
        except Exception as e:
            print(f"failed to report result: {e}")
        print(f"=== {label}: {status} ===")
        if args.once:
            return


if __name__ == "__main__":
    main()
