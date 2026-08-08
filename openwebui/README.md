# OpenWebUI pipes: Architect Council & Venture Council

## Venture Council (`venture_council.py`)

Multi-agent startup pipeline, all agents via claude-wrapper. Flow: intake
interview → **Venture Brief** framed on the Four Anchors (significant problem,
significant value, robust market, founder/timing fit) with every gap labeled
⚠️ estimate/guess → your **approve** gate → the strategist designs a dynamic
council (4 core experts — gap-scout, market-analyst with the LeanB2B factor
table, tech-feasibility, marketing-gtm — plus up to `MAX_SPECIALISTS` spawned
specialists chartered for your specific idea) → synthesis into a business plan
with formula-driven financials (3 scenarios, cost/burn/runway tables), roadmap,
Anchor Scorecard, and a **Kill/Pursue Board** (founder-only questions ranked by
impact×uncertainty with IF-YES/IF-NO branch effects — transparent, never
blocking) → a three-persona red team: skeptic (unit-economics tripwires),
investor (fundability), and operator — a bootstrapper who judges the plan as
if no raise will ever arrive, on willingness to pay, the first ten customers,
per-customer margin, and the month revenue covers burn — findings triaged back
to affected experts (spawning new specialists if needed) until all three clear
or the round cap → your plan gate → investor one-pager + 2-week validation
sprint with pre-committed decision rules + decision log.

**Plan versions & export.** Every synthesis is kept as a numbered version:
`revisions` lists them, `revision <n>` re-shows one, `diff` (or `diff 1 3`)
compares section by section, `keep <n>` restores one. `revise:` carries the
previous plan forward, so the Decision Log accumulates and answered board
questions stay answered. Each version is also written to disk — start the
coordinator with `--artifacts DIR` and every revision lands in
`DIR/<venture>/v<n>/` as `plan.md`, `tldr.md`, `brief.md`,
`kill-pursue-board.md` and one file per expert report. Turn it off with the
`EXPORT_ARTIFACTS` valve. The message itself leads with collapsible detail
and closes with a TL;DR.

**Source documents.** Attach files to the message (the 📎 in OpenWebUI) and
their full text is passed verbatim to the brief, every expert, the
synthesizer and all three red-team personas, with instructions to treat what
they contain as `[fact]` sourced to the filename rather than `[estimate]`.
Total is capped by `MAX_DOC_CHARS` (40,000). Attachments ride with the
request, so re-attach them on a rework turn if you want that round to see
them. Do **not** use a knowledge collection for this — OpenWebUI's retrieval
rewraps your message and breaks the `approve` / `revise:` / `board:` gates.

At the plan gate: **approve**, plain feedback (goes through the rework loop),
`board: <answer>` (collapses that question's branches and recomputes),
`board` / `plan` (re-show). `sessions` / `resume <id>` work via the
coordinator checkpoints. Install like the Architect Council below; key valves:
`INTERVIEW_MODEL`/`EXPERT_MODEL` (sonnet), `STRATEGIST_MODEL`/`REDTEAM_MODEL`
(opus), `MAX_SPECIALISTS`, `MAX_STRESS_ROUNDS`, `PARALLEL_EXPERTS` (off unless
your wrapper handles concurrent sessions).

Honest limits: no web access — market and competitor numbers are model
knowledge + your inputs, always labeled in the Assumption Register with a
real-world validation list.

---

# Architect Council — gated multi-model build system for OpenWebUI

Claude (via claude-wrapper) is the Principal Investigator; local Ollama models
are the review council; a fleet of worker agents on any machine does the
building. You hold the gates.

## Components

| File | Runs where | Role |
|---|---|---|
| `architect_council.py` | pasted into OpenWebUI (Admin → Functions) | the chat workflow: interview → spec gate → plan + council review → plan gate → chunk gate → dispatch → status console |
| `coordinator.py` | this Windows box (next to claude-wrapper) | zero-dependency HTTP job queue holding the chunk DAG |
| `worker.py` | Windows box, MacBook Air, Codespaces/CI, anywhere with Python + git | claims chunks, builds, tests, pushes, integrates |
| `smoke_test.py` | dev only | verifies coordinator scheduling (eligibility, auto-retry, integration gating) |

## Workflow

1. **Interview** — describe the feature. Claude drafts a short spec (goal,
   scope, non-goals, acceptance criteria) plus up to 4 questions. Iterate as
   long as you like; reply **approve** to lock it.
2. **Plan + council review** — Claude architects a design; local reviewers
   critique in parallel and Claude revises until they approve. Presented to
   you — nothing built yet. **approve** advances; feedback revises (and
   re-vets).
3. **Chunk plan** — Claude decomposes the plan into 2–6 contract-first chunks
   sized for parallel agents, sees the live worker roster, and may pin chunks
   to workers/tags. You gate again: tweak assignments in plain language
   ("assign the api chunk to macbook-air") or **approve** to dispatch.
4. **Fleet build** — workers claim eligible chunks and build each on its own
   `council/<job>/<chunk>` worktree branch (or serially on the base branch in
   `main` mode), run the chunk's tests, and push. **Continue-on-error:** a
   failed chunk is auto-requeued with its failure log fed to the next attempt,
   up to `MAX_ATTEMPTS`; the rest of the fleet keeps going.
5. **Incremental integration + e2e** — once every chunk is terminal, an
   integrator worker merges the successful branches in dependency order,
   running the test suite after *every* merge; conflicts and red tests get one
   automated repair attempt (Claude Code backend), otherwise that branch is
   rolled back and excluded rather than sinking the run. End-to-end tests run
   last. Any message in the chat refreshes the live status table; **retry**
   re-queues exhausted failures.
6. **Iterate** — after completion, describe the next feature in the same chat;
   a new interview starts with everything built so far as context.

Approval words (whole message): `approve`, `approved`, `lgtm`, `ship it`,
`proceed`, `go`, `build`, `yes`.

## Setup

### 1. Coordinator (this Windows box)

```powershell
python coordinator.py --port 8787 --token MY_SECRET
```

State persists in `council_state.json`. Open TCP 8787 in Windows Firewall so
LAN workers can reach it.

### 2. Workers (any machine that should build)

```bash
# Windows box (also the integrator, typically):
python worker.py --coordinator http://localhost:8787 --name windows-box `
    --workdir C:\council --backend claude-code --token MY_SECRET

# MacBook Air:
python3 worker.py --coordinator http://<windows-lan-ip>:8787 --name macbook-air \
    --workdir ~/council --backend claude-code --token MY_SECRET

# Online agent (Codespace/CI container, tag it so chunks can target it):
python3 worker.py --coordinator http://<tunnel-or-public>:8787 --name cloud-1 \
    --tags online --backend claude-code --token MY_SECRET --once
```

Backends: `claude-code` (best — needs the `claude` CLI authenticated on that
machine; add `--yolo` on dedicated build machines to skip permission prompts),
or `wrapper` / `ollama` with `--backend-url` (+ `--model`) for HTTP models
that emit whole files. Workers auto-tag themselves with their platform
(`windows`/`darwin`/`linux`), backend name, and every detected tool (`git`,
`npm`, `pytest`, `docker`, …), so chunk `required_tags` can target machines by
what's installed.

**Missing tools & environment errors:** workers preflight at startup (no
`git`, or `claude-code` backend without the CLI, refuses to start; an
unreachable HTTP backend warns). At runtime, a command-not-found (exit 127 /
9009, `FileNotFoundError`) is classified as an *environment* failure: it does
NOT consume a retry attempt — the coordinator re-queues the task with that
worker blocked from it, so a machine that has the tool picks it up. The chat
status view shows ⛔ lines for chunks waiting on a capable worker; dispatching
with zero workers online also warns instead of silently queueing.

### 3. Pipe function (OpenWebUI)

Paste `architect_council.py` into **Admin Panel → Functions**, enable it, then
set the valves:

- `CLAUDE_BASE_URL` / `OLLAMA_BASE_URL` / `COORDINATOR_URL` — defaults use
  `host.docker.internal` (OpenWebUI in Docker); use `localhost` otherwise.
- `REPO_URL` — the git remote workers clone/push (**required** for
  distributed builds). All workers need push access to it.
- `BUILD_MODE` — `worktree` (parallel branches + incremental integration) or
  `main` (one chunk at a time, committed straight to the base branch).
- `ALLOWED_WORKERS` / `INTEGRATOR_WORKERS` — comma-separated worker names;
  empty = anyone. Per-chunk pinning happens at the chunk gate.
- `TEST_COMMAND` / `E2E_COMMAND` — e.g. `npm test` / `npm run test:e2e`.
- `MAX_ATTEMPTS` — auto-retries per chunk before it's excluded (default 3).
- `BUILD_EXECUTION=local` — falls back to the single-model in-chat build
  (with black-box test authoring and validation) — no coordinator needed.

## Crash & lost-chat recovery

Every phase transition (spec draft, plan, chunk plan, dispatch, completion) is
checkpointed to the coordinator, which persists everything in
`council_state.json`. Recovery paths:

- **Lost/deleted chat**: in any new Architect Council chat, reply **sessions**
  to list saved workflows, then **resume <id>** to reattach at the exact gate
  it was at — including a fleet build that kept running while the chat was
  gone. Interview drafts resume from the last saved spec draft.
- **Edited/truncated chat messages**: the pipe first tries to re-read state
  from the chat, then self-heals from the checkpoint automatically.
- **Coordinator restart**: state reloads from `council_state.json`.
- **Worker crash mid-task**: its claimed task goes stale and is re-claimed by
  another worker after 2 hours (STALE_SECONDS in coordinator.py).
- **Pipe error mid-turn**: resend your last message to retry the same step.
- If the coordinator is down when a checkpoint is attempted, the reply carries
  a ⚠️ warning that the session isn't resumable until it's back.

## Notes

- State is tracked with hidden HTML comments in each assistant reply — one
  workflow per chat; don't hand-edit the plan/chunk messages.
- The council reviewers run in parallel; if two 32B models don't fit in VRAM
  set `OLLAMA_MAX_LOADED_MODELS=1` or use one reviewer.
- `complete_with_failures` means integration succeeded but some chunks were
  excluded — their logs are in the status message; reply **retry** after
  fixing the underlying issue, or fold the leftovers into the next feature.
- Workers run arbitrary build/test commands from chunk briefs — only point
  the fleet at repos and machines you trust, and keep `--yolo` off shared
  machines.
