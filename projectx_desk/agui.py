"""AG-UI event helpers for the Ask box: a TEXT-ONLY subset of the AG-UI protocol (https://docs.ag-ui.com).

Pure: no I/O, no desk state, imports nothing from the guard, client, hub, mcp or app (a test pins that). It turns the finished chat
result (`chat.Chat.ask_full`: {reply, lint, ungrounded, usage?}) into an event list, encodes events as SSE frames, and parses a
RunAgentInput request body down to the one thing the desk uses: the last user message.

Not covered on purpose: tool calls, state sync, messages snapshots, reasoning. A RunAgentInput's `tools`, `state`, `context` and
`forwardedProps` are ignored and never forwarded anywhere. Error codes are fixed words; provider or agent text never reaches a frame.
"""
import json
import uuid

MAX_BODY = 64_000          # bytes of request body
MAX_MESSAGES = 50
MAX_CONTENT = 2000         # matches chat.MAX_PROMPT: a longer last message would be refused anyway
CHUNK = 80                 # characters per TEXT_MESSAGE_CONTENT delta
FLAGS_EVENT = "px.reply_flags"
ROLES = ("user", "assistant")

# ChatError.code -> a fixed AG-UI RunError code. Anything not listed becomes "chat_error"; the original text is never sent.
CODES = {
    "chat off": "chat_off", "not_configured": "not_configured", "empty prompt": "empty_prompt", "prompt too long": "prompt_too_long",
    "key_like": "key_like", "busy": "busy", "agent timed out": "agent_timeout", "agent unreachable": "agent_unreachable",
    "agent refused": "agent_refused", "bad agent reply": "bad_agent_reply", "rate_limited": "rate_limited",
    "empty": "empty", "bad_reply": "bad_reply", "bad image": "bad_image", "too many images": "bad_image", "images need the desk agent": "bad_image",
}
MESSAGES = {c: "the Ask box could not answer (" + c + ")" for c in set(CODES.values()) | {"chat_error"}}


class AguiError(Exception):
    """A request the desk refuses before any run starts; `status` is the HTTP code, `code` a fixed word."""
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def error_code(code) -> str:
    return CODES.get(code, "chat_error") if isinstance(code, str) else "chat_error"


def parse_run_input(b) -> dict:
    """Validate a RunAgentInput object and return {threadId, runId, prompt}. Only threadId, runId and messages are read.
    The prompt is the LAST user message, exactly as /api/chat would take it. Everything else in the body is dropped."""
    if not isinstance(b, dict):
        raise AguiError("object expected")
    ids = {}
    for k in ("threadId", "runId"):
        v = b.get(k)
        if v is None:
            ids[k] = uuid.uuid4().hex
        elif isinstance(v, str) and 0 < len(v) <= 128 and v.isprintable():
            ids[k] = v
        else:
            raise AguiError("bad " + k)
    msgs = b.get("messages")
    if not isinstance(msgs, list) or not msgs or len(msgs) > MAX_MESSAGES:
        raise AguiError("bad messages")
    last_user = None
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ROLES or not isinstance(m.get("content"), str):
            raise AguiError("bad messages")
        if len(m["content"]) > MAX_CONTENT:
            raise AguiError("message too long")
        if m["role"] == "user":
            last_user = m["content"]
    if last_user is None:
        raise AguiError("no user message")
    return {"threadId": ids["threadId"], "runId": ids["runId"], "prompt": last_user}


def chunks(text: str, n: int = CHUNK) -> list:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def run_events(thread_id: str, run_id: str, result: dict, message_id: str = None) -> list:
    """RUN_STARTED, TEXT_MESSAGE_START, TEXT_MESSAGE_CONTENT x n, TEXT_MESSAGE_END, optional CUSTOM flags, RUN_FINISHED.
    `result` is chat's already-redacted result; the reply is only chunked here, never rewritten."""
    mid = message_id or uuid.uuid4().hex
    reply = result.get("reply") if isinstance(result, dict) else None
    reply = reply if isinstance(reply, str) else ""
    ev = [{"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id},
          {"type": "TEXT_MESSAGE_START", "messageId": mid, "role": "assistant"}]
    ev += [{"type": "TEXT_MESSAGE_CONTENT", "messageId": mid, "delta": d} for d in chunks(reply) if d]
    ev.append({"type": "TEXT_MESSAGE_END", "messageId": mid})
    flags = {"lint": result.get("lint") or [], "ungrounded": result.get("ungrounded") or [], "guard": result.get("guard") or []}
    if isinstance(result.get("usage"), dict):
        flags["usage"] = result["usage"]
    if flags["lint"] or flags["ungrounded"] or flags["guard"] or "usage" in flags:
        ev.append({"type": "CUSTOM", "name": FLAGS_EVENT, "value": flags})
    ev.append({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})
    return ev


def error_events(thread_id: str, run_id: str, code) -> list:
    c = error_code(code)
    return [{"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id},
            {"type": "RUN_ERROR", "message": MESSAGES[c], "code": c}]


def sse(event: dict) -> str:
    return "data: " + json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n\n"
