"""
title: Architect Council (Claude PI + Local Model Council + Worker Fleet)
author: claude-wrapper
version: 0.3.0
license: MIT
description: >-
  Gated multi-model workflow: Claude (via claude-wrapper) interviews you into a
  feature spec, architects a plan vetted by local Ollama models, then (with your
  approval at every gate) decomposes the feature into chunks and dispatches them
  to a coordinator where worker agents on any machine (this PC, a MacBook,
  online) build in parallel on git worktree branches or serially on main,
  auto-retry on failure, then incrementally integrate and run e2e tests.
requirements: aiohttp
"""

import asyncio
import json
import re
import uuid
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Workflow states (embedded as hidden markers in every assistant reply)
# ---------------------------------------------------------------------------

STATE_DISCOVERY = "discovery"      # interviewing the user, drafting the spec
STATE_PLAN_REVIEW = "plan_review"  # plan produced + council-vetted, awaiting user gate
STATE_CHUNK_REVIEW = "chunk_review"  # chunk breakdown awaiting user gate
STATE_BUILDING = "building"        # dispatched to the fleet; turns poll status
STATE_DONE = "done"                # feature built; next message starts a new cycle

STATE_MARKER = "<!-- ac:state={state} -->"
STATE_RE = re.compile(r"<!-- ac:state=(\w+) -->")
JOB_MARKER = "<!-- ac:job={job} -->"
JOB_RE = re.compile(r"<!-- ac:job=(\w+) -->")
SESSION_MARKER = "<!-- ac:session={session} -->"
SESSION_RE = re.compile(r"<!-- ac:session=(\w+) -->")
APPROVE_RE = re.compile(
    r"^\s*(approve[d]?|lgtm|ship it|proceed|go|build|yes)\s*[.!]*\s*$", re.IGNORECASE
)
RETRY_RE = re.compile(r"^\s*retry\s*[.!]*\s*$", re.IGNORECASE)
SESSIONS_RE = re.compile(r"^\s*sessions\s*$", re.IGNORECASE)
RESUME_RE = re.compile(r"^\s*resume\s+(\w+)\s*$", re.IGNORECASE)

class StopRun(Exception):
    """User pressed stop (client disconnected) — halt between phases."""


SPEC_HEADING = "# 📄 Feature Spec"
PLAN_HEADING = "# 📐 Proposed Plan"
CHUNKS_HEADING = "# 🧩 Chunk Plan"
RECORDS_HEADING = "# 📋 Council Records"

# ---------------------------------------------------------------------------
# Role prompts
# ---------------------------------------------------------------------------

DISCOVERY_SYSTEM = """You are the Principal Investigator running a requirements
interview. Your goal is a SHORT feature spec the user will sign off on.

From the conversation so far, respond in markdown with exactly:
## Draft Feature Spec
- **Goal**: one sentence
- **Scope**: bullet list of what is included
- **Non-goals**: what is explicitly out of scope
- **Acceptance criteria**: verifiable bullets

## Open Questions
Up to 4 targeted questions whose answers would change the spec. If everything
important is already known, write "None — this spec looks ready to approve."

Keep the spec tight (under ~300 words). Update it every turn to reflect the
user's latest answers. If the conversation contains previously built features,
treat them as the existing system this feature builds on."""

SPEC_FINALIZE_SYSTEM = """You are the Principal Investigator. The user has
approved the draft spec. Write the FINAL locked feature spec from the
conversation: Goal, Scope, Non-goals, Acceptance criteria. No questions, no
commentary — just the spec in markdown bullets, under ~300 words."""

ARCHITECT_SYSTEM = """You are the Principal Investigator and Lead Architect.
You will receive an approved feature spec (and possibly context about
previously built features it extends). Produce a rigorous design.

Respond with a design document in markdown using exactly these sections:
## Problem Statement
## Assumptions & Constraints
## Proposed Architecture
## Components & Responsibilities
## Data Flow
## Key Decisions & Trade-offs
## Risks & Mitigations
## Implementation Plan

Be concrete: name technologies, interfaces, and data shapes. Stay within the
spec's scope — do not design non-goals."""

REVIEWER_SYSTEM = """You are a skeptical senior design reviewer on a review board.
You will receive a feature spec and a proposed architecture written by the
lead architect. Your job is to find real flaws: missing requirements, scaling
problems, security gaps, over-engineering, unclear interfaces, wrong technology
choices, scope creep beyond the spec.

Rules:
- Be specific and cite the section you are criticizing.
- Do NOT rewrite the design; list numbered findings.
- If the design is sound, say so briefly.
- Your FINAL line must be exactly `VERDICT: APPROVE` or `VERDICT: REVISE`."""

REVISION_SYSTEM = """You are the Principal Investigator and Lead Architect.
Your design received findings (from the review board and/or the user). Address
every finding: either change the design or explicitly rebut it with
justification.

Output the FULL revised design document (same section headings as before),
followed by a `## Review Responses` section that addresses each finding."""

DECOMPOSE_SYSTEM = """You are the Principal Investigator planning parallel
execution by a fleet of autonomous coding agents. You will receive a feature
spec, an approved architecture, the build mode, and a roster of available
worker agents. Break the implementation into 2-6 chunks.

Rules:
- Contract-first: each chunk's brief must fully define every interface it
  exposes or consumes (function signatures, schemas, routes, file formats), so
  chunks can be built in parallel against contracts, not each other's code.
- Each brief must be complete standalone instructions for an agent that sees
  NOTHING else: exact files to create/modify, behavior, edge cases, and the
  unit tests the agent must write for its own chunk.
- If the feature needs end-to-end tests, give one chunk the job of writing
  them (they will be run at integration time).
- depends_on expresses INTEGRATION order (what must merge first).
- In "main" build mode chunks execute one at a time in depends_on order
  directly on the base branch, so later chunks may use earlier chunks' code.
- Optionally pin a chunk to specific workers (assign_to) or capabilities
  (required_tags), using ONLY names/tags that appear in the roster.

Output ONLY a JSON object, no prose:
{"chunks": [{"id": "snake_case_id", "title": "short title",
  "brief": "complete standalone instructions",
  "depends_on": ["other_id"], "test_command": "command or omit",
  "assign_to": ["worker-name"], "required_tags": ["tag"]}]}
depends_on/test_command/assign_to/required_tags are optional per chunk."""

BUILDER_SYSTEM = """You are the implementation engineer. You will receive a
feature spec and an approved architecture. Produce a concrete implementation:
project structure, key source files with real code (not pseudocode), and
configuration. Follow the architecture exactly; where it is silent, make the
simplest reasonable choice and note it. Output markdown with fenced code
blocks, each preceded by its file path."""

TEST_AUTHOR_SYSTEM = """You are the test engineer. You will receive a feature
spec and an approved architecture — deliberately NOT the implementation, so
your tests stay black-box. Write a runnable test suite that verifies every
acceptance criterion in the spec plus the key interfaces in the design:
happy paths, error paths, and edge cases.

Use the natural test framework for the design's technology stack (e.g. pytest,
jest, go test). Output markdown: a short "test plan" table mapping each
acceptance criterion to test names, then the test files as fenced code blocks,
each preceded by its file path."""

VALIDATOR_SYSTEM = """You are the validation engineer. You will receive a
feature spec, an approved architecture, an implementation, and (if provided) a
test suite. Check the implementation:
- Does every component in the design exist in the implementation?
- Do interfaces/data shapes match the design?
- Are the spec's acceptance criteria met?
- Walk through each test in the suite against the code: which would fail, and why?
- Any obvious bugs, missing error handling, or security issues?

List numbered findings citing file/component/test names. Your FINAL line must
be exactly `VERDICT: PASS` or `VERDICT: FAIL`."""

REPAIR_SYSTEM = """You are the implementation engineer. A validator reviewed
your implementation against the spec, architecture, and test suite and found
problems. Fix every finding — the tests are the contract, so make the code
pass them rather than arguing with them — and output the FULL corrected
implementation (same format: file paths followed by fenced code blocks)."""


class Pipe:
    class Valves(BaseModel):
        CLAUDE_BASE_URL: str = Field(
            default="http://host.docker.internal:8000/v1",
            description="claude-wrapper OpenAI-compatible base URL. Use http://localhost:8000/v1 if OpenWebUI runs outside Docker.",
        )
        CLAUDE_API_KEY: str = Field(
            default="",
            description="API key for claude-wrapper (leave empty if auth is disabled).",
        )
        CLAUDE_MODEL: str = Field(
            default="sonnet",
            description="Claude model id exposed by the wrapper (e.g. sonnet, opus).",
        )
        OLLAMA_BASE_URL: str = Field(
            default="http://host.docker.internal:11434/v1",
            description="Ollama OpenAI-compatible base URL. Use http://localhost:11434/v1 outside Docker.",
        )
        REVIEWER_MODELS: str = Field(
            default="deepseek-r1:32b,qwen2.5:32b",
            description="Comma-separated local models that vet the design (run in parallel).",
        )
        MAX_REVIEW_ROUNDS: int = Field(
            default=2,
            description="Max council review/revision rounds per plan before presenting it anyway.",
        )
        COUNCIL_ON_USER_FEEDBACK: bool = Field(
            default=True,
            description="Re-run the council after the plan is revised from user feedback.",
        )
        # ---- execution ----
        BUILD_EXECUTION: str = Field(
            default="distributed",
            description="'distributed' = chunk the plan and dispatch to the worker fleet; 'local' = single in-chat builder model.",
        )
        BUILD_MODE: str = Field(
            default="worktree",
            description="'worktree' = parallel chunk branches + incremental integration; 'main' = serialized commits directly on the base branch.",
        )
        COORDINATOR_URL: str = Field(
            default="http://host.docker.internal:8787",
            description="Coordinator (coordinator.py) base URL.",
        )
        COORDINATOR_TOKEN: str = Field(
            default="", description="Coordinator auth token, if it was started with --token."
        )
        REPO_URL: str = Field(
            default="",
            description="Git remote workers clone/push (required for distributed builds).",
        )
        BASE_BRANCH: str = Field(default="main", description="Branch chunks branch from / merge into.")
        ALLOWED_WORKERS: str = Field(
            default="",
            description="Comma-separated worker names allowed to build this job (empty = any worker).",
        )
        INTEGRATOR_WORKERS: str = Field(
            default="",
            description="Comma-separated worker names allowed to run integration (empty = any worker).",
        )
        TEST_COMMAND: str = Field(
            default="", description="Default test command for chunks and incremental integration."
        )
        E2E_COMMAND: str = Field(
            default="", description="End-to-end test command run after integration."
        )
        MAX_ATTEMPTS: int = Field(
            default=3,
            description="Continue-on-error: auto-retries per chunk (failure log fed to the next attempt).",
        )
        # ---- local (in-chat) build fallback ----
        BUILDER_MODEL: str = Field(
            default="qwen2.5-coder:32b",
            description="Local build: model that implements the approved design in-chat.",
        )
        VALIDATOR_MODEL: str = Field(
            default="devstral:24b",
            description="Local build: model that validates the implementation.",
        )
        TESTER_MODEL: str = Field(
            default="qwen2.5-coder:32b",
            description="Local build: model that writes a black-box test suite.",
        )
        ENABLE_TESTING: bool = Field(
            default=True, description="Local build: write a test suite and use it during validation."
        )
        ENABLE_VALIDATION: bool = Field(
            default=True, description="Local build: run the validate/repair phase."
        )
        # ---- misc ----
        SHOW_INTERMEDIATE: bool = Field(
            default=True,
            description="Include reviewer critiques and validation reports as collapsible sections.",
        )
        HISTORY_CHAR_BUDGET: int = Field(
            default=60000,
            description="Max characters of conversation history passed to models (oldest dropped first).",
        )
        REQUEST_TIMEOUT: int = Field(
            default=600,
            description="Per-request timeout in seconds (Claude via the wrapper can be slow).",
        )
        STOP_ON_DISCONNECT: bool = Field(
            default=False,
            description=(
                "Poll the client connection between phases and halt if it looks disconnected. "
                "Off by default: on some OpenWebUI/uvicorn builds is_disconnected() reports a "
                "false positive once the request body is consumed, which stops every run at its "
                "first phase. Pressing Stop cancels the pipe task either way."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "architect-council", "name": "Architect Council (Claude PI)"}]

    # -- HTTP helpers -------------------------------------------------------

    async def _chat(self, session, base_url, api_key, model, messages) -> str:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model, "messages": messages, "stream": False},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"{model} @ {base_url} returned {resp.status}: {text[:500]}")
            data = await resp.json()
            return self._unwrap_completion(data["choices"][0]["message"]["content"])

    @staticmethod
    def _unwrap_completion(text: str) -> str:
        """claude-wrapper sometimes double-encodes: the content field itself
        holds a serialized chat-completion JSON — occasionally MALFORMED
        (model-authored envelope with bad escaping). Unwrap until prose."""
        for _ in range(3):
            s = text.strip()
            if not (s.startswith("{") and '"choices"' in s):
                break
            try:
                text = json.loads(s)["choices"][0]["message"]["content"]
                continue
            except Exception:
                pass
            m = re.search(r'"content"\s*:\s*"(.*)"\s*\}\s*,\s*"finish_reason"',
                          s, re.DOTALL)
            if not m:
                break
            raw = m.group(1)
            try:
                text = json.loads(f'"{raw}"')
            except Exception:
                text = (raw.replace("\\n", "\n").replace('\\"', '"')
                        .replace("\\t", "\t").replace("\\\\", "\\"))
        return text

    async def _claude(self, session, system: str, user: str) -> str:
        return await self._chat(
            session,
            self.valves.CLAUDE_BASE_URL,
            self.valves.CLAUDE_API_KEY,
            self.valves.CLAUDE_MODEL,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
        )

    async def _local(self, session, model: str, system: str, user: str) -> str:
        text = await self._chat(
            session,
            self.valves.OLLAMA_BASE_URL,
            "",
            model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        # Reasoning models (e.g. deepseek-r1) wrap chain-of-thought in <think> tags.
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    async def _coord(self, session, method: str, path: str, payload=None):
        headers = {"Content-Type": "application/json"}
        if self.valves.COORDINATOR_TOKEN:
            headers["X-Auth-Token"] = self.valves.COORDINATOR_TOKEN
        url = self.valves.COORDINATOR_URL.rstrip("/") + path
        async with session.request(
            method, url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status == 204:
                return None
            if resp.status >= 400:
                raise RuntimeError(f"coordinator {method} {path} -> {resp.status}: {await resp.text()}")
            return await resp.json()

    # -- conversation/state helpers -----------------------------------------

    @staticmethod
    def _content_str(content) -> str:
        if isinstance(content, list):  # multimodal message
            return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return content or ""

    def _scan_marker(self, messages: list, regex) -> Optional[str]:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                found = regex.findall(self._content_str(m.get("content")))
                if found:
                    return found[-1]
        return None

    def _last_assistant(self, messages: list) -> str:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                return self._content_str(m.get("content"))
        return ""

    def _last_user(self, messages: list) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return self._content_str(m.get("content"))
        return ""

    def _history(self, messages: list) -> str:
        parts = []
        for m in messages:
            role, content = m.get("role"), self._content_str(m.get("content"))
            if not content or role == "system":
                continue
            parts.append(f"{role.upper()}: {content}")
        text, budget = [], self.valves.HISTORY_CHAR_BUDGET
        for p in reversed(parts):
            if budget - len(p) < 0:
                text.append("[earlier conversation truncated]")
                break
            text.append(p)
            budget -= len(p)
        return "\n\n".join(reversed(text))

    @staticmethod
    def _extract_section(text: str, heading: str) -> str:
        start = text.rfind(heading)
        if start == -1:
            return ""
        body = text[start + len(heading):]
        for stop in (SPEC_HEADING, PLAN_HEADING, CHUNKS_HEADING, RECORDS_HEADING, "\n---\n"):
            idx = body.find(stop)
            if idx != -1:
                body = body[:idx]
        body = re.sub(r"</?details>|<summary>.*?</summary>", "", body)
        return body.strip()

    @staticmethod
    def _extract_json_block(text: str):
        blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
        return blocks[-1] if blocks else None

    @staticmethod
    def _verdict(text: str, approve_word: str) -> bool:
        matches = re.findall(r"VERDICT:\s*(\w+)", text, re.IGNORECASE)
        return bool(matches) and matches[-1].upper() == approve_word

    @staticmethod
    def _collapsible(title: str, content: str) -> str:
        return f"<details>\n<summary>{title}</summary>\n\n{content}\n\n</details>\n"

    @staticmethod
    def _footer(state: str, text: str, extra_marker: str = "") -> str:
        return f"\n\n---\n> {text}\n{STATE_MARKER.format(state=state)}{extra_marker}"

    @staticmethod
    def _csv(value: str) -> list:
        return [x.strip() for x in value.split(",") if x.strip()]

    # -- session persistence (survive lost chats / crashes) -------------------

    @staticmethod
    def _name_from(text: str) -> str:
        first = next((ln for ln in text.strip().splitlines() if ln.strip()), "feature")
        return re.sub(r"[#*`\-\s]+", " ", first).strip()[:60] or "feature"

    async def _save_session(self, session, sid: str, phase: str, **data) -> str:
        """Checkpoint to the coordinator. Never fails the turn — returns a
        warning string (or empty) to append to the reply."""
        try:
            await self._coord(session, "POST", "/sessions",
                              {"session_id": sid, "phase": phase, **data})
            return ""
        except Exception as e:
            return f"\n\n⚠️ *Checkpoint not saved (coordinator unreachable: {e}) — this session cannot be resumed if the chat is lost.*"

    async def _list_sessions(self, session) -> str:
        try:
            sessions = await self._coord(session, "GET", "/sessions")
        except Exception as e:
            return f"**Cannot list sessions** — coordinator unreachable at `{self.valves.COORDINATOR_URL}`:\n```\n{e}\n```"
        if not sessions:
            return "No saved sessions yet. Describe a feature to start one."
        rows = "\n".join(
            f"| `{sid}` | {s.get('name', '')} | `{s.get('phase', '?')}` "
            f"| {s.get('job_id') or '—'} | {s.get('updated_seconds_ago', '?')}s ago |"
            for sid, s in sorted(sessions.items(),
                                 key=lambda kv: kv[1].get("updated_seconds_ago", 0))
        )
        return (
            "# 💾 Saved sessions\n\n"
            "| id | feature | phase | job | updated |\n|---|---|---|---|---|\n"
            f"{rows}\n\n"
            "Reply **resume <id>** to continue one from where it left off."
        )

    async def _load_checkpoint(self, session, sid: str) -> dict:
        try:
            return await self._coord(session, "GET", f"/sessions/{sid}")
        except Exception:
            return {}

    async def _resume_session(self, session, sid: str) -> str:
        try:
            data = await self._coord(session, "GET", f"/sessions/{sid}")
        except Exception as e:
            return (
                f"**Cannot resume `{sid}`** — not found or coordinator unreachable:\n```\n{e}\n```\n"
                "Reply **sessions** to list what's available."
            )
        phase = data.get("phase", "discovery")
        smark = SESSION_MARKER.format(session=sid)
        header = f"# 🔁 Resumed `{sid}` — {data.get('name', '')} *(phase: {phase})*\n\n"

        if data.get("job_id") and phase in (STATE_BUILDING, STATE_DONE):
            try:
                job = await self._coord(session, "GET", f"/jobs/{data['job_id']}")
                return header + self._status_message(job, data["job_id"]) + smark
            except Exception as e:
                return header + (
                    f"The dispatched job `{data['job_id']}` could not be fetched: `{e}`. "
                    "Check the coordinator's council_state.json."
                    + self._footer(STATE_DISCOVERY, "💬 Describe a feature to start fresh.", smark)
                )
        if phase == STATE_CHUNK_REVIEW and data.get("spec") and data.get("chunks"):
            roster = await self._worker_roster(session)
            return header + self._chunks_message(
                data["spec"], data.get("design", ""), data["chunks"], roster
            ) + smark
        if phase == STATE_PLAN_REVIEW and data.get("spec"):
            return header + self._plan_message(
                data["spec"], data.get("design", ""), True, []
            ) + smark
        if phase == STATE_DONE:
            return header + "This feature was completed." + self._footer(
                STATE_DONE, "🎉 Describe the next feature to build on top of it.", smark
            )
        # discovery (or partial data): restore the draft spec and keep interviewing
        draft = data.get("draft", "(no draft was saved yet)")
        return header + f"## Draft Feature Spec (restored)\n\n{draft}" + self._footer(
            STATE_DISCOVERY,
            "💬 Answer the questions or request changes to iterate. Reply **approve** when the spec is right.",
            smark,
        )

    # -- council phase -------------------------------------------------------

    async def _run_council(self, session, status, spec: str, design: str, sections: list):
        v = self.valves
        reviewers = self._csv(v.REVIEWER_MODELS)
        approved = False
        for round_no in range(1, v.MAX_REVIEW_ROUNDS + 1):
            await status(f"🔍 Council round {round_no}/{v.MAX_REVIEW_ROUNDS}: {', '.join(reviewers)}…")
            review_input = f"# Feature Spec\n{spec}\n\n# Proposed Design\n{design}"
            results = await asyncio.gather(
                *(self._local(session, m, REVIEWER_SYSTEM, review_input) for m in reviewers),
                return_exceptions=True,
            )
            critiques = []
            for model, res in zip(reviewers, results):
                if isinstance(res, Exception):
                    critiques.append((model, f"(reviewer unavailable: {res})", True))
                else:
                    critiques.append((model, res, self._verdict(res, "APPROVE")))

            if v.SHOW_INTERMEDIATE:
                for model, text, ok in critiques:
                    icon = "✅" if ok else "🛠️"
                    sections.append(self._collapsible(f"{icon} Council round {round_no} — {model}", text))

            if all(ok for _, _, ok in critiques):
                approved = True
                break

            await status(f"✏️ {v.CLAUDE_MODEL} is revising the design…")
            findings = "\n\n".join(
                f"### Findings from {model}\n{text}" for model, text, ok in critiques if not ok
            )
            design = await self._claude(
                session,
                REVISION_SYSTEM,
                f"# Feature Spec\n{spec}\n\n# Your Current Design\n{design}\n\n# Reviewer Findings\n{findings}",
            )
        return design, approved

    def _plan_message(self, spec: str, design: str, approved: bool, sections: list) -> str:
        v = self.valves
        verdict_line = (
            "**Design approved by the review board.**"
            if approved
            else f"**Presented after {v.MAX_REVIEW_ROUNDS} council round(s) without full approval** — remaining objections are in the council records."
        )
        output = f"{SPEC_HEADING}\n\n{spec}\n\n{PLAN_HEADING}\n\n{verdict_line}\n\n{design}\n"
        if sections:
            output += f"\n---\n{RECORDS_HEADING}\n\n" + "\n".join(sections)
        gate = (
            "🚦 **Gate: your call.** Reply **approve** to break this plan into chunks for the worker fleet, or reply with feedback to revise it."
            if v.BUILD_EXECUTION == "distributed"
            else "🚦 **Gate: your call.** Reply **approve** to build this plan, or reply with feedback to revise it."
        )
        output += self._footer(STATE_PLAN_REVIEW, gate)
        return output

    # -- distributed execution ----------------------------------------------

    async def _worker_roster(self, session) -> str:
        try:
            workers = await self._coord(session, "GET", "/workers")
            if not workers:
                return "(no workers have checked in yet)"
            return "\n".join(
                f"- {name}: tags={w.get('tags', [])}, platform={w.get('platform', '?')}, "
                f"last seen {w.get('seen_seconds_ago', '?')}s ago"
                for name, w in workers.items()
            )
        except Exception as e:
            return f"(coordinator unreachable: {e})"

    async def _decompose(self, session, spec, design, roster, feedback=None, prev=None) -> list:
        v = self.valves
        user = (
            f"# Feature Spec\n{spec}\n\n# Architecture\n{design}\n\n"
            f"# Build mode\n{v.BUILD_MODE}\n\n# Worker roster\n{roster}"
        )
        if feedback:
            user += (
                f"\n\n# Previous chunk plan\n```json\n{prev}\n```\n\n"
                f"# User feedback on the chunk plan\n{feedback}"
            )
        raw = await self._claude(session, DECOMPOSE_SYSTEM, user)
        for _ in range(2):
            block = self._extract_json_block(raw) or raw
            try:
                chunks = json.loads(block)["chunks"]
                for c in chunks:
                    c["id"] = re.sub(r"[^\w-]", "_", str(c["id"]))
                return chunks
            except Exception as e:
                raw = await self._claude(
                    session,
                    DECOMPOSE_SYSTEM,
                    user + f"\n\n# Error\nYour previous output could not be parsed ({e}). "
                    "Output ONLY the corrected JSON object.",
                )
        raise RuntimeError("could not get valid chunk JSON from the architect")

    def _chunks_message(self, spec: str, design: str, chunks: list, roster: str) -> str:
        v = self.valves
        rows = "\n".join(
            f"| `{c['id']}` | {c['title']} | {', '.join(c.get('depends_on', [])) or '—'} "
            f"| {', '.join(c.get('assign_to', [])) or 'any'} "
            f"| {', '.join(c.get('required_tags', [])) or '—'} |"
            for c in chunks
        )
        briefs = "\n".join(self._collapsible(f"🧩 {c['id']} — {c['title']}", c["brief"]) for c in chunks)
        mode_note = (
            f"**Build mode:** `worktree` — {len(chunks)} chunks build in parallel on their own "
            "branches, then merge incrementally (tests after every merge) plus e2e."
            if v.BUILD_MODE == "worktree"
            else f"**Build mode:** `main` — {len(chunks)} chunks build one at a time, in dependency "
            f"order, committing directly to `{v.BASE_BRANCH}`."
        )
        return (
            f"{SPEC_HEADING}\n\n{spec}\n\n"
            f"{PLAN_HEADING}\n\n{design}\n\n"
            f"{CHUNKS_HEADING}\n\n{mode_note}\n\n"
            f"**Available workers:**\n{roster}\n\n"
            f"| chunk | title | merge after | assigned to | required tags |\n"
            f"|---|---|---|---|---|\n{rows}\n\n{briefs}\n"
            f"```json\n{json.dumps({'chunks': chunks}, indent=2)}\n```\n"
            + self._footer(
                STATE_CHUNK_REVIEW,
                "🚦 **Gate: your call.** Reply **approve** to dispatch these chunks to the fleet. "
                "Or reply with changes — e.g. *\"merge chunks 1 and 2\"*, *\"assign the api chunk to macbook-air\"*.",
            )
        )

    async def _dispatch(self, session, spec: str, design: str, chunks: list) -> str:
        v = self.valves
        name = re.sub(r"[#*`\-\s]+", " ", spec.strip().splitlines()[0]).strip()[:60] or "feature"
        for c in chunks:
            c["brief"] = (
                f"{c['brief']}\n\n--- SHARED CONTEXT ---\n\n"
                f"# Feature Spec\n{spec}\n\n# Architecture\n{design}"
            )
        payload = {
            "name": name,
            "repo_url": v.REPO_URL,
            "base_branch": v.BASE_BRANCH,
            "build_mode": v.BUILD_MODE,
            "test_command": v.TEST_COMMAND,
            "e2e_command": v.E2E_COMMAND,
            "max_attempts": v.MAX_ATTEMPTS,
            "allowed_workers": self._csv(v.ALLOWED_WORKERS),
            "integrator_workers": self._csv(v.INTEGRATOR_WORKERS),
            "chunks": chunks,
        }
        result = await self._coord(session, "POST", "/jobs", payload)
        return result["job_id"]

    def _status_message(self, job: dict, job_id: str) -> str:
        icons = {"pending": "⏳", "claimed": "🔄", "done": "✅", "failed": "❌"}
        rows = "\n".join(
            f"| {icons.get(c['status'], '❓')} {c['status']} | `{c['id']}` | {c['title']} "
            f"| {c.get('worker') or '—'} | {c.get('attempts', 0)} | `{c['branch']}` |"
            for c in job["chunks"]
        )
        out = (
            f"# 🛰️ Fleet status — {job['name']} (`{job_id}`)\n\n"
            f"**Overall:** `{job['status']}`\n\n"
            f"| status | chunk | title | worker | attempts | branch |\n"
            f"|---|---|---|---|---|---|\n{rows}\n"
        )
        integ = job["integration"]
        if job["build_mode"] == "worktree":
            out += (
                f"\n**Integration:** {icons.get(integ['status'], '❓')} `{integ['status']}`"
                f"{' — ' + integ['worker'] if integ.get('worker') else ''}"
                f" → branch `{integ['branch']}`\n"
            )
        blocked = [
            f"⛔ `{c['id']}` cannot run on {', '.join(c['blocked_workers'])} (missing tools there) — waiting for a capable worker"
            for c in job["chunks"] if c.get("blocked_workers") and c["status"] != "done"
        ]
        if integ.get("blocked_workers") and integ["status"] != "done":
            blocked.append(
                f"⛔ integration cannot run on {', '.join(integ['blocked_workers'])} (missing tools there)"
            )
        if blocked:
            out += "\n" + "\n".join(blocked) + "\n"
        logs = [
            self._collapsible(f"📜 {c['id']} log ({c['status']}, attempts: {c.get('attempts', 0)})", f"```\n{c['log']}\n```")
            for c in job["chunks"] if c.get("log")
        ]
        if integ.get("log"):
            logs.append(self._collapsible(f"📜 integration log ({integ['status']})", f"```\n{integ['log']}\n```"))
        if logs:
            out += "\n" + "\n".join(logs)

        marker = JOB_MARKER.format(job=job_id)
        if job["status"] in ("complete", "complete_with_failures"):
            done_note = (
                f"🎉 **Feature complete.** Integrated on `{integ['branch']}` — review and merge it into "
                f"`{job['base_branch']}`."
                if job["build_mode"] == "worktree"
                else f"🎉 **Feature complete.** Chunks were committed directly to `{job['base_branch']}`."
            )
            if job["status"] == "complete_with_failures":
                done_note += " ⚠️ Some chunks were excluded after exhausting retries — see logs above."
            out += self._footer(
                STATE_DONE,
                done_note + " Describe the next feature to build on top of this.",
                marker,
            )
        elif job["status"] == "needs_attention":
            out += self._footer(
                STATE_BUILDING,
                "⚠️ Retries exhausted on some tasks. Reply **retry** to re-queue them, or any message to refresh.",
                marker,
            )
        else:
            out += self._footer(
                STATE_BUILDING,
                "🔄 The fleet is working. Send any message to refresh status (failed chunks auto-retry).",
                marker,
            )
        return out

    # -- local (in-chat) build fallback --------------------------------------

    async def _run_local_build(self, session, status, spec: str, design: str, sections: list):
        v = self.valves

        async def write_tests():
            return await self._local(
                session, v.TESTER_MODEL, TEST_AUTHOR_SYSTEM,
                f"# Feature Spec\n{spec}\n\n# Approved Architecture\n{design}",
            )

        async def build():
            return await self._local(
                session, v.BUILDER_MODEL, BUILDER_SYSTEM,
                f"# Feature Spec\n{spec}\n\n# Approved Architecture\n{design}",
            )

        tests = None
        if v.ENABLE_TESTING:
            await status(f"🔨 {v.BUILDER_MODEL} is building, {v.TESTER_MODEL} is writing tests…")
            implementation, tests_res = await asyncio.gather(build(), write_tests(), return_exceptions=True)
            if isinstance(implementation, Exception):
                raise implementation
            if isinstance(tests_res, Exception):
                sections.append(f"⚠️ Test authoring failed: `{tests_res}`\n")
            else:
                tests = tests_res
        else:
            await status(f"🔨 {v.BUILDER_MODEL} is building the implementation…")
            implementation = await build()

        if v.ENABLE_VALIDATION:
            await status(f"🧪 {v.VALIDATOR_MODEL} is validating the build…")
            try:
                validation_input = (
                    f"# Feature Spec\n{spec}\n\n# Architecture\n{design}\n\n# Implementation\n{implementation}"
                )
                if tests:
                    validation_input += f"\n\n# Test Suite\n{tests}"
                report = await self._local(session, v.VALIDATOR_MODEL, VALIDATOR_SYSTEM, validation_input)
                passed = self._verdict(report, "PASS")
                if v.SHOW_INTERMEDIATE:
                    icon = "✅" if passed else "❌"
                    sections.append(self._collapsible(f"{icon} Validation report — {v.VALIDATOR_MODEL}", report))
                if not passed:
                    await status(f"🔧 {v.BUILDER_MODEL} is repairing the build…")
                    repair_input = (
                        f"# Feature Spec\n{spec}\n\n# Architecture\n{design}\n\n"
                        f"# Your Implementation\n{implementation}\n\n# Validator Findings\n{report}"
                    )
                    if tests:
                        repair_input += f"\n\n# Test Suite (make the code pass these)\n{tests}"
                    implementation = await self._local(session, v.BUILDER_MODEL, REPAIR_SYSTEM, repair_input)
            except Exception as e:
                sections.append(f"⚠️ Validation phase failed: `{e}`\n")
        return implementation, tests

    # -- main entry ---------------------------------------------------------

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __request__: Optional[Any] = None,
    ) -> str:
        v = self.valves

        async def status(msg: str, done: bool = False):
            # optional stop check between phases — off by default, see
            # STOP_ON_DISCONNECT; pressing stop cancels this task regardless
            if v.STOP_ON_DISCONNECT and __request__ is not None and not done:
                try:
                    if await __request__.is_disconnected():
                        raise StopRun()
                except StopRun:
                    raise
                except Exception:
                    pass  # older OpenWebUI without is_disconnected
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": msg, "done": done}}
                )

        messages = body.get("messages", [])
        state = self._scan_marker(messages, STATE_RE) or STATE_DISCOVERY
        user_msg = self._last_user(messages)
        approved = bool(APPROVE_RE.match(user_msg))
        existing_sid = self._scan_marker(messages, SESSION_RE)
        sid = existing_sid or uuid.uuid4().hex[:8]
        smark = SESSION_MARKER.format(session=sid)

        if not user_msg.strip():
            return (
                "Describe the feature or system you want to build and I'll start the "
                "requirements interview. (Or reply **sessions** to resume a previous workflow.)"
            )

        try:
            async with aiohttp.ClientSession() as session:

                # ---- RECOVERY: list/resume checkpointed sessions ----
                if SESSIONS_RE.match(user_msg):
                    return await self._list_sessions(session)
                resume = RESUME_RE.match(user_msg)
                if resume:
                    await status(f"🔁 Resuming session {resume.group(1)}…", done=True)
                    return await self._resume_session(session, resume.group(1))

                # ---- DISCOVERY: interview until the user approves the spec ----
                if state == STATE_DISCOVERY and not approved:
                    await status(f"🧠 {v.CLAUDE_MODEL} is refining the spec…")
                    draft = await self._claude(
                        session, DISCOVERY_SYSTEM,
                        f"# Conversation so far\n{self._history(messages)}",
                    )
                    extra = {} if existing_sid else {"name": self._name_from(user_msg)}
                    note = await self._save_session(
                        session, sid, STATE_DISCOVERY, draft=draft, **extra
                    )
                    await status("Done", done=True)
                    return draft + self._footer(
                        STATE_DISCOVERY,
                        "💬 Answer the questions or request changes to iterate. Reply **approve** when the spec is right.",
                        smark,
                    ) + note

                # ---- SPEC APPROVED: architect + council, then gate on user ----
                if state == STATE_DISCOVERY and approved:
                    await status(f"📄 {v.CLAUDE_MODEL} is locking the spec…")
                    spec = await self._claude(
                        session, SPEC_FINALIZE_SYSTEM,
                        f"# Conversation so far\n{self._history(messages)}",
                    )
                    await status(f"📐 {v.CLAUDE_MODEL} (architect) is designing…")
                    design = await self._claude(session, ARCHITECT_SYSTEM, f"# Feature Spec\n{spec}")
                    sections: list = []
                    design, ok = await self._run_council(session, status, spec, design, sections)
                    note = await self._save_session(
                        session, sid, STATE_PLAN_REVIEW,
                        name=self._name_from(spec), spec=spec, design=design,
                    )
                    await status("Plan ready — awaiting your approval", done=True)
                    return self._plan_message(spec, design, ok, sections) + smark + note

                # ---- PLAN GATE ----
                if state == STATE_PLAN_REVIEW:
                    last = self._last_assistant(messages)
                    spec = self._extract_section(last, SPEC_HEADING)
                    design = self._extract_section(last, PLAN_HEADING)
                    if not spec or not design:  # self-heal from the checkpoint
                        cp = await self._load_checkpoint(session, sid)
                        spec = spec or cp.get("spec", "")
                        design = design or cp.get("design", "")
                    if not spec or not design:
                        return (
                            "I lost track of the current spec/plan and no checkpoint was found. "
                            "Reply **sessions** to resume another workflow, or restate the feature."
                            + self._footer(STATE_DISCOVERY, "💬 Describe the feature to begin again.")
                        )

                    if not approved:
                        await status(f"✏️ {v.CLAUDE_MODEL} is revising the plan from your feedback…")
                        design = await self._claude(
                            session, REVISION_SYSTEM,
                            f"# Feature Spec\n{spec}\n\n# Your Current Design\n{design}\n\n"
                            f"# Reviewer Findings\n### Findings from the user\n{user_msg}",
                        )
                        sections = []
                        ok = True
                        if v.COUNCIL_ON_USER_FEEDBACK:
                            design, ok = await self._run_council(session, status, spec, design, sections)
                        note = await self._save_session(
                            session, sid, STATE_PLAN_REVIEW, spec=spec, design=design,
                        )
                        await status("Revised plan ready — awaiting your approval", done=True)
                        return self._plan_message(spec, design, ok, sections) + smark + note

                    # approved →
                    if v.BUILD_EXECUTION == "distributed":
                        if not v.REPO_URL:
                            return (
                                "**Cannot dispatch:** set the `REPO_URL` valve (git remote the workers "
                                "clone and push), or switch `BUILD_EXECUTION` to `local`."
                                + self._footer(STATE_PLAN_REVIEW, "Fix the valve, then reply **approve** again.")
                            )
                        await status(f"🧩 {v.CLAUDE_MODEL} is decomposing the plan into chunks…")
                        roster = await self._worker_roster(session)
                        chunks = await self._decompose(session, spec, design, roster)
                        note = await self._save_session(
                            session, sid, STATE_CHUNK_REVIEW,
                            spec=spec, design=design, chunks=chunks,
                        )
                        await status("Chunk plan ready — awaiting your approval", done=True)
                        return self._chunks_message(spec, design, chunks, roster) + smark + note

                    sections = []
                    implementation, tests = await self._run_local_build(session, status, spec, design, sections)
                    output = f"# 🔨 Implementation\n\n{implementation}\n"
                    if tests:
                        output += f"\n# 🧪 Test Suite\n\n{tests}\n"
                    if sections:
                        output += f"\n---\n{RECORDS_HEADING}\n\n" + "\n".join(sections)
                    output += self._footer(
                        STATE_DONE,
                        "🎉 **Feature complete.** Describe the next feature to build on top of this.",
                        smark,
                    )
                    note = await self._save_session(session, sid, STATE_DONE, spec=spec, design=design)
                    await status("Done", done=True)
                    return output + note

                # ---- CHUNK GATE ----
                if state == STATE_CHUNK_REVIEW:
                    last = self._last_assistant(messages)
                    spec = self._extract_section(last, SPEC_HEADING)
                    design = self._extract_section(last, PLAN_HEADING)
                    prev_json = self._extract_json_block(last)
                    if not (spec and design and prev_json):  # self-heal from the checkpoint
                        cp = await self._load_checkpoint(session, sid)
                        spec = spec or cp.get("spec", "")
                        design = design or cp.get("design", "")
                        if not prev_json and cp.get("chunks"):
                            prev_json = json.dumps({"chunks": cp["chunks"]})
                    if not (spec and design and prev_json):
                        return (
                            "I lost track of the chunk plan and no checkpoint was found. "
                            "Reply **sessions** to resume another workflow, or restate the feature."
                            + self._footer(STATE_DISCOVERY, "💬 Describe the feature to begin again.")
                        )

                    if not approved:
                        await status(f"🧩 {v.CLAUDE_MODEL} is revising the chunk plan…")
                        roster = await self._worker_roster(session)
                        chunks = await self._decompose(
                            session, spec, design, roster, feedback=user_msg, prev=prev_json
                        )
                        note = await self._save_session(
                            session, sid, STATE_CHUNK_REVIEW,
                            spec=spec, design=design, chunks=chunks,
                        )
                        await status("Chunk plan ready — awaiting your approval", done=True)
                        return self._chunks_message(spec, design, chunks, roster) + smark + note

                    await status("🚀 Dispatching chunks to the fleet…")
                    chunks = json.loads(prev_json)["chunks"]
                    job_id = await self._dispatch(session, spec, design, chunks)
                    note = await self._save_session(
                        session, sid, STATE_BUILDING, job_id=job_id, spec=spec, design=design,
                    )
                    job = await self._coord(session, "GET", f"/jobs/{job_id}")
                    await status("Dispatched", done=True)
                    warning = ""
                    try:
                        workers = await self._coord(session, "GET", "/workers")
                        if not workers:
                            warning = (
                                "⚠️ **No workers have checked in yet** — chunks will wait in the "
                                "queue until a `worker.py` connects to the coordinator.\n\n"
                            )
                    except Exception:
                        pass
                    return warning + self._status_message(job, job_id) + smark + note

                # ---- BUILDING: every message refreshes the fleet status ----
                if state == STATE_BUILDING:
                    job_id = self._scan_marker(messages, JOB_RE)
                    if not job_id:  # self-heal from the checkpoint
                        cp = await self._load_checkpoint(session, sid)
                        job_id = cp.get("job_id")
                    if not job_id:
                        return (
                            "I lost the job id for this build and no checkpoint was found. "
                            "Reply **sessions** to find it, or check the coordinator's `council_state.json`."
                            + self._footer(STATE_DISCOVERY, "💬 Describe the feature to begin again.")
                        )
                    if RETRY_RE.match(user_msg):
                        await status("♻️ Re-queuing failed tasks…")
                        await self._coord(session, "POST", f"/jobs/{job_id}/retry")
                    await status("📡 Fetching fleet status…")
                    job = await self._coord(session, "GET", f"/jobs/{job_id}")
                    if job["status"] in ("complete", "complete_with_failures"):
                        await self._save_session(session, sid, STATE_DONE, job_id=job_id)
                    await status("Done", done=True)
                    return self._status_message(job, job_id) + smark

                # ---- DONE: any message starts the next feature cycle ----
                await status(f"🧠 {v.CLAUDE_MODEL} is starting the next feature interview…")
                draft = await self._claude(
                    session, DISCOVERY_SYSTEM,
                    f"# Conversation so far (includes previously built features)\n{self._history(messages)}",
                )
                # next feature = fresh checkpoint, so the finished one stays resumable
                sid = uuid.uuid4().hex[:8]
                smark = SESSION_MARKER.format(session=sid)
                note = await self._save_session(
                    session, sid, STATE_DISCOVERY,
                    name=self._name_from(user_msg), draft=draft,
                )
                await status("Done", done=True)
                return draft + self._footer(
                    STATE_DISCOVERY,
                    "💬 Answer the questions or request changes to iterate. Reply **approve** when the spec is right.",
                    smark,
                ) + note

        except StopRun:
            return (
                "⏹️ **Stopped at your request.**"
                + self._footer(state, "Resend your last message to continue from where we left off.")
            )
        except Exception as e:
            await status("Failed", done=True)
            return (
                f"**Pipeline error** — check that claude-wrapper (`{v.CLAUDE_BASE_URL}`), "
                f"Ollama (`{v.OLLAMA_BASE_URL}`), and the coordinator (`{v.COORDINATOR_URL}`) "
                f"are reachable.\n\n```\n{e}\n```"
                + self._footer(state, "Resend your last message to retry from where we left off.")
            )
