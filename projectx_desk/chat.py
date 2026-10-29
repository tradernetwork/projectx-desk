"""Desk chat: relays one typed question to the read-only desk agent (scripts/deskagent.py serve) and returns its text.

The agent is a separate process (`claude -p`, no Bash/Edit/click) and this module is only a relay: it holds no desk state, has no
path to the guard, and the question and the reply are plain text. Off unless PX_CHAT_URL is set, and the URL must be loopback so
the desk can never be turned into a forwarder to somewhere else. Errors are fixed codes, never the agent's or the network's text.
"""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .net import is_loopback

ROOT = Path(__file__).resolve().parent.parent
MAX_PROMPT = 2000
MAX_REPLY = 8000
SESSION = "desk"                    # fixed server side: the browser cannot pick or enumerate agent sessions
TIMEOUT_S = 300.0


class ChatError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code, self.status = code, status


def config(environ=None):
    """(url, token) or None when chat is off. A non-loopback URL or an unreadable token is off, never a guess."""
    env = os.environ if environ is None else environ
    url = (env.get("PX_CHAT_URL") or "").strip().rstrip("/")
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme != "http" or not is_loopback(parts.hostname or ""):
        return None
    token = (env.get("PX_CHAT_TOKEN") or "").strip()
    if not token:
        path = Path(env.get("PX_CHAT_TOKEN_FILE") or ROOT / "data" / "agent_token")
        try:
            token = path.read_text().strip()
        except OSError:
            return None
    return (url, token) if len(token) >= 32 else None


class Chat:
    def __init__(self, cfg, transport=None):
        self.cfg, self.transport = cfg, transport
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.cfg is not None

    async def ask(self, prompt) -> str:
        if self.cfg is None:
            raise ChatError("chat off", 503)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ChatError("empty prompt", 400)
        if len(prompt) > MAX_PROMPT:
            raise ChatError("prompt too long", 400)
        if self.lock.locked():
            raise ChatError("busy", 429)           # the agent runs one question at a time
        async with self.lock:
            url, token = self.cfg
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_S) as c:
                    r = await c.post(url + "/ask", json={"prompt": prompt.strip(), "session": SESSION},
                                     headers={"Authorization": "Bearer " + token})
            except httpx.TimeoutException:
                raise ChatError("agent timed out", 504)
            except httpx.HTTPError:
                raise ChatError("agent unreachable", 502)
            if r.status_code != 200:
                raise ChatError("agent refused", 502)
            try:
                reply = r.json().get("result")
            except ValueError:
                reply = None
            if not isinstance(reply, str):
                raise ChatError("bad agent reply", 502)
            return reply[:MAX_REPLY]
