"""
title: Two-Phase Pipeline (template)
author: me
version: 0.1.0
description: >-
  Minimal multi-agent pipeline skeleton: model A drafts, the user gates,
  model B executes. Copy, rename ids/markers, add phases and roles.
requirements: aiohttp
"""

import json
import re
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

# --- state machine markers (rename 'tp' to your pipe's short id) -----------
STATE_DRAFT = "draft"      # iterating with the user on the draft
STATE_DONE = "done"        # executed; next message starts a new cycle
STATE_MARKER = "<!-- tp:state={state} -->"
STATE_RE = re.compile(r"<!-- tp:state=(\w+) -->")
APPROVE_RE = re.compile(r"^\s*(approve[d]?|lgtm|proceed|go|yes)\s*[.!]*\s*$", re.I)

DRAFT_HEADING = "# 📝 Draft"

DRAFTER_SYSTEM = """You are the planner. From the conversation, produce/update
a short actionable draft of what will be executed. End with open questions if
any. Keep it under 300 words."""

EXECUTOR_SYSTEM = """You are the executor. You will receive an approved draft.
Carry it out completely and output the full result in markdown."""


class Pipe:
    class Valves(BaseModel):
        DRAFTER_BASE_URL: str = Field(
            default="http://host.docker.internal:8000/v1",
            description="OpenAI-compatible endpoint for the drafting model "
            "(claude-wrapper). Use http://localhost:8000/v1 outside Docker.",
        )
        DRAFTER_API_KEY: str = Field(default="", description="Bearer key if required.")
        DRAFTER_MODEL: str = Field(default="sonnet", description="Drafting model id.")
        EXECUTOR_BASE_URL: str = Field(
            default="http://host.docker.internal:11434/v1",
            description="OpenAI-compatible endpoint for the executor (Ollama).",
        )
        EXECUTOR_MODEL: str = Field(default="qwen2.5-coder:32b", description="Executor model id.")
        REQUEST_TIMEOUT: int = Field(default=600, description="Per-request timeout (s).")
        HISTORY_CHAR_BUDGET: int = Field(
            default=60000, description="Max history characters passed to models."
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "two-phase-pipeline", "name": "Two-Phase Pipeline"}]

    # --- generic OpenAI-compatible caller ----------------------------------

    async def _chat(self, session, base_url, api_key, model, system, user) -> str:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model, "stream": False, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"{model} @ {base_url} -> {resp.status}: {(await resp.text())[:500]}"
                )
            text = (await resp.json())["choices"][0]["message"]["content"]
            # claude-wrapper sometimes double-encodes: content holds a
            # serialized completion JSON — unwrap until we hit prose
            for _ in range(3):
                s = text.strip()
                if not (s.startswith("{") and '"choices"' in s):
                    break
                try:
                    text = json.loads(s)["choices"][0]["message"]["content"]
                except Exception:
                    break
            # reasoning models wrap chain-of-thought in <think> tags
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # --- history/state helpers ---------------------------------------------

    @staticmethod
    def _content_str(content) -> str:
        if isinstance(content, list):
            return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return content or ""

    def _scan_state(self, messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                found = STATE_RE.findall(self._content_str(m.get("content")))
                if found:
                    return found[-1]
        return STATE_DRAFT

    def _last_user(self, messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return self._content_str(m.get("content"))
        return ""

    def _last_assistant(self, messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                return self._content_str(m.get("content"))
        return ""

    def _history(self, messages) -> str:
        parts = [
            f"{m['role'].upper()}: {self._content_str(m.get('content'))}"
            for m in messages
            if m.get("role") != "system" and self._content_str(m.get("content"))
        ]
        out, budget = [], self.valves.HISTORY_CHAR_BUDGET
        for p in reversed(parts):
            if budget - len(p) < 0:
                out.append("[earlier conversation truncated]")
                break
            out.append(p)
            budget -= len(p)
        return "\n\n".join(reversed(out))

    @staticmethod
    def _extract_section(text: str, heading: str) -> str:
        start = text.rfind(heading)
        if start == -1:
            return ""
        body = text[start + len(heading):]
        idx = body.find("\n---\n")
        return (body[:idx] if idx != -1 else body).strip()

    @staticmethod
    def _footer(state: str, text: str) -> str:
        return f"\n\n---\n> {text}\n{STATE_MARKER.format(state=state)}"

    # --- main entry ---------------------------------------------------------

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
    ) -> str:
        v = self.valves

        async def status(msg, done=False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": msg, "done": done}}
                )

        messages = body.get("messages", [])
        state = self._scan_state(messages)
        user_msg = self._last_user(messages)
        approved = bool(APPROVE_RE.match(user_msg))

        if not user_msg.strip():
            return "Tell me what you want done and I'll draft it first."

        try:
            async with aiohttp.ClientSession() as session:

                # phase 1: draft & iterate until the user approves
                if state != STATE_DRAFT or not approved:
                    await status(f"📝 {v.DRAFTER_MODEL} is drafting…")
                    draft = await self._chat(
                        session, v.DRAFTER_BASE_URL, v.DRAFTER_API_KEY, v.DRAFTER_MODEL,
                        DRAFTER_SYSTEM, f"# Conversation\n{self._history(messages)}",
                    )
                    await status("Done", done=True)
                    return f"{DRAFT_HEADING}\n\n{draft}" + self._footer(
                        STATE_DRAFT,
                        "💬 Reply with changes to iterate, or **approve** to execute.",
                    )

                # phase 2 (gated): execute the approved draft
                draft = self._extract_section(self._last_assistant(messages), DRAFT_HEADING)
                if not draft:
                    return "I lost the draft — please restate the task." + self._footer(
                        STATE_DRAFT, "💬 Describe the task to start over."
                    )
                await status(f"⚙️ {v.EXECUTOR_MODEL} is executing…")
                result = await self._chat(
                    session, v.EXECUTOR_BASE_URL, "", v.EXECUTOR_MODEL,
                    EXECUTOR_SYSTEM, f"# Approved Draft\n{draft}",
                )
                await status("Done", done=True)
                return f"# ✅ Result\n\n{result}" + self._footer(
                    STATE_DONE, "🎉 Done. Describe the next task to start a new cycle."
                )

        except Exception as e:
            await status("Failed", done=True)
            return (
                f"**Pipeline error** — check `{v.DRAFTER_BASE_URL}` and "
                f"`{v.EXECUTOR_BASE_URL}` are reachable.\n\n```\n{e}\n```"
                + self._footer(state, "Resend your last message to retry.")
            )
