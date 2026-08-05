"""
title: Claude Wrapper
author: ahnm
version: 5.0.0
description: Open WebUI pipe for ChrisColeTech/claude-wrapper, with per-chat session continuity and effort / permission-mode control.
"""

import json
import logging
from typing import Any, Dict, Generator, List, Union

import requests
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Mirrors the wrapper's /v1/efforts and /v1/permission-modes endpoints.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
BASE_MODELS = [("sonnet", "Claude Sonnet"), ("opus", "Claude Opus"), ("fable", "Claude Fable")]


class Pipe:
    class Valves(BaseModel):
        CLAUDE_WRAPPER_URL: str = Field(
            default="http://localhost:8000",
            description="Base URL of the claude-wrapper service (no trailing slash).",
        )
        BEARER_TOKEN: str = Field(
            default="",
            description="API key for claude-wrapper, if one is configured.",
        )
        DEFAULT_EFFORT: str = Field(
            default="medium",
            description=f"Effort used when the selected model does not specify one. One of: {', '.join(EFFORTS)}.",
        )
        PERMISSION_MODE: str = Field(
            default="",
            description="Permission mode passed to the Claude CLI. Leave empty to use the CLI default.",
        )
        REQUEST_TIMEOUT: int = Field(
            default=300,
            description="HTTP timeout in seconds. Must exceed the wrapper's own timeout (120s by default) or you will time out client-side first.",
        )
        SESSION_CONTINUITY: bool = Field(
            default=True,
            description="Send the Open WebUI chat id as session_id so the wrapper keeps model/effort/permission mode per chat.",
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, Any]]:
        """Base models, plus a model:effort variant per effort level."""
        entries = [{"id": model_id, "name": name} for model_id, name in BASE_MODELS]

        for model_id, name in BASE_MODELS:
            for effort in EFFORTS:
                entries.append({"id": f"{model_id}:{effort}", "name": f"{name} ({effort})"})

        return entries

    def pipe(
        self, body: dict, __user__: dict = None, __metadata__: dict = None
    ) -> Union[str, Generator[str, None, None]]:
        # Open WebUI prefixes the manifold id, e.g. "claude_wrapper.opus:high"
        raw_model = body.get("model", "")
        model_id = raw_model.split(".")[-1] if "." in raw_model else raw_model

        # Split our own "model:effort" encoding so we can send explicit fields.
        # Sending them explicitly avoids relying on the wrapper's precedence rules.
        model, _, effort = model_id.partition(":")
        effort = effort or self.valves.DEFAULT_EFFORT

        payload: Dict[str, Any] = {
            "model": model,
            "messages": body.get("messages", []),
            "stream": body.get("stream", True),
        }

        if effort:
            payload["effort"] = effort

        if self.valves.PERMISSION_MODE:
            payload["permission_mode"] = self.valves.PERMISSION_MODE

        # The wrapper remembers model/effort/permission mode per session_id, so a
        # chat keeps its settings even if a later request omits them.
        if self.valves.SESSION_CONTINUITY and __metadata__:
            chat_id = __metadata__.get("chat_id")
            if chat_id:
                payload["session_id"] = str(chat_id)

        headers = {"Content-Type": "application/json"}
        if self.valves.BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.BEARER_TOKEN}"

        url = f"{self.valves.CLAUDE_WRAPPER_URL}/v1/chat/completions"

        if payload["stream"]:
            return self._stream(url, headers, payload)

        return self._complete(url, headers, payload)

    def _complete(self, url: str, headers: dict, payload: dict) -> str:
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=self.valves.REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            # The wrapper returns a standard OpenAI completion. Return the text
            # itself; returning the envelope renders raw JSON in the chat.
            choices = data.get("choices") or []
            if choices:
                return choices[0].get("message", {}).get("content", "") or ""

            if "error" in data:
                return f"claude-wrapper error: {data['error'].get('message', data['error'])}"

            logger.warning("Unexpected response shape from claude-wrapper: %s", data)
            return ""

        except requests.Timeout:
            return (
                f"claude-wrapper timed out after {self.valves.REQUEST_TIMEOUT}s. "
                "Higher effort levels take longer; raise REQUEST_TIMEOUT."
            )
        except Exception as exc:
            return f"Error communicating with claude-wrapper: {exc}"

    def _stream(self, url: str, headers: dict, payload: dict) -> Generator[str, None, None]:
        """Yield plain content deltas; Open WebUI renders them as tokens."""
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.valves.REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            for line in response.iter_lines():
                if not line:
                    continue

                line_str = line.decode("utf-8").strip()
                if not line_str.startswith("data:"):
                    continue

                data_content = line_str[5:].strip()
                if data_content == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_content)
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON SSE payload: %s", data_content)
                    continue

                if "error" in chunk:
                    yield f"\n\nclaude-wrapper error: {chunk['error'].get('message', chunk['error'])}"
                    return

                for choice in chunk.get("choices", []):
                    content = choice.get("delta", {}).get("content")
                    if content:
                        yield content

        except requests.Timeout:
            yield (
                f"\n\nclaude-wrapper timed out after {self.valves.REQUEST_TIMEOUT}s. "
                "Higher effort levels take longer; raise REQUEST_TIMEOUT."
            )
        except Exception as exc:
            yield f"\n\nError communicating with claude-wrapper: {exc}"
