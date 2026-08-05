# Open WebUI Integration

[Open WebUI](https://github.com/open-webui/open-webui) talks to claude-wrapper through a
**pipe function**, which lets it send fields a stock OpenAI client cannot — most importantly
`session_id`, `effort`, and `permission_mode`.

The function lives at [`openwebui-pipe.py`](openwebui-pipe.py).

## Why a pipe function is needed

Open WebUI's chat UI sends a standard OpenAI request body. That is enough to get a reply, but
three things the wrapper supports have nowhere to live in it:

| Field | What it does | Without the pipe |
| --- | --- | --- |
| `session_id` | Wrapper remembers model / effort / permission mode per chat | Every request starts cold |
| `effort` | Maps to the Claude CLI `--effort` flag | CLI default only |
| `permission_mode` | Maps to `--permission-mode` | CLI default only |

The pipe injects all three. It uses Open WebUI's `chat_id` as the `session_id`, so each chat
keeps its own settings.

## Installation

1. In Open WebUI, go to **Workspace → Functions → +** (new function).
2. Paste the contents of [`openwebui-pipe.py`](openwebui-pipe.py) and save.
3. Enable the function.
4. Open its **valves** and set `CLAUDE_WRAPPER_URL` to your wrapper (default
   `http://localhost:8000`).

The models then appear in the model picker.

## Valves

| Valve | Default | Notes |
| --- | --- | --- |
| `CLAUDE_WRAPPER_URL` | `http://localhost:8000` | No trailing slash. |
| `BEARER_TOKEN` | *(empty)* | Only if the wrapper is started with an API key. |
| `DEFAULT_EFFORT` | `medium` | Used when the selected model carries no effort suffix. |
| `PERMISSION_MODE` | *(empty)* | Empty means the CLI default. |
| `REQUEST_TIMEOUT` | `300` | **Must exceed the wrapper's own timeout** (120s by default), or the client aborts first and you lose the real error. |
| `SESSION_CONTINUITY` | `true` | Sends `chat_id` as `session_id`. |

## Model list

The pipe advertises each base model plus one `model:effort` variant per effort level, so effort
can be switched from the model picker without touching valves:

```
sonnet, opus, fable
sonnet:low … sonnet:max
opus:low … opus:max
fable:low … fable:max
```

Selecting `opus:high` sends `model: "opus"` and `effort: "high"` as separate fields. The pipe
splits the suffix itself rather than passing `opus:high` through for the wrapper to decode —
the wrapper accepts both, but an explicit field takes precedence over a model-name segment, so
splitting in the pipe stops the `DEFAULT_EFFORT` valve from silently overriding a dropdown
choice.

To keep the picker usable, permission mode is not enumerated as a variant; set it via the
valve. The wrapper does accept it as a third segment (`opus:high:plan`) for clients that ask.

## Verifying it works

Confirm the wrapper is running a build that has these routes — an older process will accept
`effort` and `session_id` and silently ignore them rather than erroring:

```bash
curl http://localhost:8000/v1/efforts
# {"object":"list","data":["low","medium","high","xhigh","max"]}
```

After sending one message in a chat, the session should exist and carry the settings:

```bash
curl http://localhost:8000/v1/sessions/<open-webui-chat-id>
# {"session_id":"…","model":"opus","effort":"high", …}
```

## Troubleshooting

**Raw JSON appears in the chat.** The non-streaming path must return the message text, not the
response envelope — `choices[0].message.content`, not `json.dumps(response)`.

**Tokens render oddly or not at all.** Open WebUI versions differ in whether a pipe generator
should yield plain content strings or raw `data:` SSE lines. This function yields plain strings.
If your build expects SSE, yield the untouched line instead:

```python
yield f"{line_str}\n\n"
```

and drop the JSON parsing in `_stream`.

**Client-side timeouts on high effort.** Raise `REQUEST_TIMEOUT` above the wrapper's timeout.

**Settings are not remembered between messages.** Check `SESSION_CONTINUITY` is on, and that
`session_id` — not `user` — is in the payload. The wrapper does not read `user`.
