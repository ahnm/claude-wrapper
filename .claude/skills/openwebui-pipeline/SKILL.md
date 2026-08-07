---
name: openwebui-pipeline
description: >-
  Scaffold or modify multi-agent/multi-model pipeline Pipe functions for
  OpenWebUI — orchestrating Claude (via claude-wrapper) and local Ollama models
  through phases, user approval gates, and turn-based state machines. Use when
  the user wants a new OpenWebUI function/pipe that chains multiple models,
  adds roles (architect/reviewer/builder/validator), adds approval gates, or
  needs multi-turn workflow state. Reference implementation:
  openwebui/architect_council.py.
---

# Building multi-agent OpenWebUI pipelines

An OpenWebUI **Pipe function** is a Python class pasted into Admin → Functions.
It appears as a model in the picker; every chat turn calls `pipe()` with the
full message history. That makes it the right place to orchestrate multi-model
pipelines — but it is **stateless between turns** and **cannot touch the
host filesystem**, which drives most of the patterns below.

Study `openwebui/architect_council.py` before writing anything — it is the
canonical implementation of every pattern here. For work that must touch git,
run tests, or span machines, do NOT put it in the pipe: dispatch to the
coordinator/worker system (`openwebui/coordinator.py`, `openwebui/worker.py`).

## Process

1. **Preview the design and iterate — do NOT write code yet.** Present a
   design preview (format below) and ask what to change. Revise the preview
   through as many rounds as the user wants; only implement after they
   approve it. This mirrors the gates the pipes themselves use.
2. Once approved, start from `references/pipe_template.py` (minimal two-phase
   pipe with one gate) or extend `architect_council.py`.
3. Syntax-check with `python -c "import ast; ast.parse(open(f).read())"` —
   the file cannot be executed locally (OpenWebUI imports it server-side).
4. Give the user install steps: paste into Admin → Functions, enable, set
   Valves.

## Design preview format

Show all three parts — the goal is that the user can *see the pipeline
running* before a line of code exists:

1. **Phase diagram** — one line per phase: trigger → role(model) → output →
   what advances it. Mark user gates explicitly:

   ```
   1. Interview      user msg   → drafter(sonnet)        → draft spec     ⟳ iterate
   2. 🚦 GATE        "approve"  → locks spec
   3. Review         auto       → reviewers(r1, qwen)    → verdicts       ⟳ until unanimous
   4. Build          auto       → builder(qwen-coder)    → implementation → done
   ```

2. **Valve table** — every knob with its default (endpoints, models, rounds,
   toggles).

3. **Mock chat transcript** — a short fake conversation showing exactly what
   the user will see each turn: the status lines while phases run, the shape
   of each reply (headings, tables, collapsibles), and the footer with the
   gate options. Example turn:

   > *(status: 🔍 Council round 1/2: deepseek-r1:32b…)*
   >
   > `# 📐 Proposed Plan` … `<details>reviewer critiques</details>`
   >
   > ---
   > 🚦 **Gate: your call.** Reply **approve** to build, or reply with
   > feedback to revise.

When the user requests changes, re-show the *whole updated preview* (not a
diff) so they always gate on the full picture.

## Pipe anatomy (required shape)

```python
"""
title: My Pipeline            <- docstring frontmatter, shown in the UI
description: ...
requirements: aiohttp         <- pip deps OpenWebUI installs
"""
class Pipe:
    class Valves(BaseModel):  # every endpoint/model/knob is a Valve
        ...
    def __init__(self):
        self.valves = self.Valves()
    def pipes(self):          # registers entries in the model picker
        return [{"id": "my-pipeline", "name": "My Pipeline"}]
    async def pipe(self, body: dict, __user__=None, __event_emitter__=None) -> str:
        ...                   # return the full markdown reply
```

## Non-negotiable rules

- **All configuration in Valves** (pydantic `Field` with `description`), never
  hardcoded: base URLs, API keys, model names, round limits, timeouts,
  feature toggles. Default URLs to `http://host.docker.internal:<port>/v1`
  (OpenWebUI usually runs in Docker) and document the `localhost` alternative
  in the Field description.
- **One generic OpenAI-compatible caller** (`POST {base}/chat/completions`,
  `stream: false`, aiohttp, timeout from a Valve) reused for claude-wrapper
  (port 8000), Ollama (port 11434, path `/v1`), or anything else.
- **Strip reasoning tags** from local-model output before parsing:
  `re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)` — deepseek-r1
  and friends will otherwise break your verdict parsing.
- **Unwrap double-encoded completions**: claude-wrapper can return the real
  content as a serialized completion JSON *inside* the content field. After
  extracting content, loop: while it starts with `{` and contains
  `"choices"`, `json.loads` it and take `choices[0].message.content` again
  (see the template's `_chat`). Without this the chat shows raw JSON.
- **Never let an exception escape `pipe()`** — catch everything, return a
  markdown error naming which endpoint likely failed, and include the state
  footer so the user can "resend your last message to retry".
- **Emit progress** during long phases:
  `await __event_emitter__({"type": "status", "data": {"description": msg, "done": bool}})`.
- Text the pipe returns is the assistant message verbatim — markdown, tables,
  and `<details><summary>` collapsibles all render.

## Multi-turn state machine (the core trick)

Pipes get no storage, but they get full chat history each turn. Persist state
inside your own replies as hidden HTML comments and read it back:

- Append `<!-- myid:state=phase_name -->` to every reply; recover it by
  scanning `reversed(messages)` for the last assistant message that matches.
  Same trick for any small id (job id, session id).
- Store large artifacts (a spec, a plan, JSON) as **visible sections** under
  unique headings (`# 📄 Spec`), and re-extract with `text.rfind(heading)` up
  to the next known heading. Embed machine-readable data as a ```json fenced
  block and take the *last* one.
- **Approval gates**: a phase transition happens only when the user's whole
  message matches an approval regex
  (`^\s*(approve|lgtm|proceed|yes)\s*[.!]*\s*$`, case-insensitive) — so
  "yes, but change X" correctly counts as feedback, not approval. Any
  non-approval message is iteration feedback for the current phase. State the
  gate options in a footer line of every reply.
- Reserve command words sparingly (`retry`, `sessions`, `resume <id>`) and
  match them the same whole-message way.

## Multi-model orchestration patterns

- **Role prompts as module constants**: one focused system prompt per role
  (architect, reviewer, builder, validator, decomposer…). Reviewers/validators
  must end with a machine-parsable verdict line (`VERDICT: APPROVE|REVISE`);
  parse the **last** `VERDICT:` match, case-insensitive.
- **Fan-out**: `asyncio.gather(*calls, return_exceptions=True)` and treat a
  failed reviewer as approving (with a note) rather than sinking the run.
- **Review loop**: reviewers critique → lead model revises addressing every
  finding → repeat until unanimous or a Valve-capped round count; present the
  outcome either way, flagging unresolved objections.
- **Black-box testing role**: have the test-author see only spec+design (not
  the implementation) so tests check requirements, not the code.
- **Context passing**: flatten history to `"ROLE: content"` lines under a
  char budget Valve, dropping oldest first; append shared context (spec,
  design) to each sub-agent prompt — sub-agents see nothing else.
- Keep intermediate outputs (critiques, validation reports) in
  `<details>` collapsibles behind a `SHOW_INTERMEDIATE` valve.

## Durability (if the workflow matters)

Chat-embedded state dies with the chat. For resumable workflows, checkpoint
each phase transition to the coordinator's session store
(`POST /sessions` with `session_id` — it merge-upserts) and support
`sessions` / `resume <id>` commands. Self-heal: when re-extraction from chat
fails, fall back to the checkpoint before giving up. See
`_save_session` / `_resume_session` in architect_council.py.

## Checklist before delivering

- [ ] `ast.parse` passes
- [ ] every knob is a Valve with a description
- [ ] every reply ends with a footer stating the user's options + state marker
- [ ] approval regex is whole-message; feedback path exists in every gated phase
- [ ] exceptions cannot escape `pipe()`
- [ ] long phases emit status events
- [ ] README/install/valve notes updated (openwebui/README.md)
