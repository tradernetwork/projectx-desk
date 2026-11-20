"""Desk chat: relays one typed question to the read-only desk agent (scripts/deskagent.py serve) and returns its text.

The agent is a separate process (`claude -p`, no Bash/Edit/click) and this module is only a relay: it holds no desk state, has no
path to the guard, and the question and the reply are plain text. Off unless PX_CHAT_URL is set, and the URL must be loopback so
the desk can never be turned into a forwarder to somewhere else. Errors are fixed codes, never the agent's or the network's text.
"""
import asyncio
import base64
import binascii
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import ground, lint, llm_chat, llm_config, redact, reply_guard, secretcheck
from .net import is_loopback

ROOT = Path(__file__).resolve().parent.parent
MAX_PROMPT = 2000
MAX_REPLY = 8000
MAX_IMAGES = 3
MAX_BODY = 17_000_000                 # bytes of request: three 4 MB images as base64 plus the text
MAX_SIDE, MAX_PIXELS = 8000, 40_000_000   # a small file can declare a gigapixel canvas that the agent's image reader would try to decode
MAX_IMAGE_BYTES = 4_000_000          # decoded, per screenshot; the browser downsizes anything bigger
MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
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
    token, name = (env.get("PX_CHAT_TOKEN") or "").strip(), "PX_CHAT_TOKEN"
    if not token:
        path = Path(env.get("PX_CHAT_TOKEN_FILE") or ROOT / "data" / "agent_token")
        name = "PX_CHAT_TOKEN_FILE" if env.get("PX_CHAT_TOKEN_FILE") else "data/agent_token"
        try:
            token = path.read_text().strip()
        except OSError:
            return None
    why = secretcheck.weak_secret_reason(token)
    if why:
        print(f"Ask relay off: {name} is not usable ({why}); generate one with: openssl rand -hex 32", flush=True)   # the name and reason, never the value
        return None
    return url, token


def dims(blob: bytes, ext: str):
    """(width, height) read from the header, or None when it cannot be read. Header bytes only: nothing is decoded here."""
    try:
        if ext == "png" and blob[12:16] == b"IHDR":
            return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        if ext == "gif":
            return int.from_bytes(blob[6:8], "little"), int.from_bytes(blob[8:10], "little")
        if ext == "jpg":
            i = 2
            while i + 9 < len(blob):
                if blob[i] != 0xFF:
                    return None
                m = blob[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                n = int.from_bytes(blob[i + 2:i + 4], "big")
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(blob[i + 7:i + 9], "big"), int.from_bytes(blob[i + 5:i + 7], "big")
                i += 2 + n
        if ext == "webp":
            kind = blob[12:16]
            if kind == b"VP8X":
                return 1 + int.from_bytes(blob[24:27], "little"), 1 + int.from_bytes(blob[27:30], "little")
            if kind == b"VP8 ":
                return int.from_bytes(blob[26:28], "little") & 0x3FFF, int.from_bytes(blob[28:30], "little") & 0x3FFF
            if kind == b"VP8L" and blob[20] == 0x2F:
                b = int.from_bytes(blob[21:25], "little")
                return 1 + (b & 0x3FFF), 1 + ((b >> 14) & 0x3FFF)
    except (IndexError, ValueError):
        return None
    return None


def dims_ok(blob: bytes, ext: str) -> bool:
    wh = dims(blob, ext)
    return bool(wh) and 0 < wh[0] <= MAX_SIDE and 0 < wh[1] <= MAX_SIDE and wh[0] * wh[1] <= MAX_PIXELS


def clean_images(images) -> list:
    """Screenshots from the browser as [{data: base64}]. Only real PNG/JPEG/GIF/WebP bytes within the caps get through, and what is relayed is
    the sniffed type plus the base64 (never a filename or a type the browser claimed). Anything else is a fixed 400."""
    if images in (None, []):
        return []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ChatError("too many images", 400)
    out = []
    for im in images:
        raw = im.get("data") if isinstance(im, dict) else None
        if not isinstance(raw, str) or len(raw) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise ChatError("bad image", 400)
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ChatError("bad image", 400)
        if not blob or len(blob) > MAX_IMAGE_BYTES:
            raise ChatError("bad image", 400)
        ext = next((e for m, e in MAGIC if blob.startswith(m)), None)
        if ext is None and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            ext = "webp"
        if ext is None or not dims_ok(blob, ext):
            raise ChatError("bad image", 400)
        out.append({"ext": ext, "data": raw})
    return out


class Chat:
    def __init__(self, cfg, transport=None):
        self.cfg, self.transport = cfg, transport
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        if llm_config.route() == "http":
            return llm_config.resolve() is not None        # a configured HTTP provider needs no agent process
        return self.cfg is not None

    async def ask(self, prompt, images=None) -> str:
        """The reply text only (redacted). `ask_full` adds the soft flags."""
        return (await self.ask_full(prompt, images))["reply"]

    async def ask_full(self, prompt, images=None, digest=None) -> dict:
        """{reply, lint, ungrounded}. The typed prompt is redacted before it is relayed and a prompt that is itself a key is refused
        (`key_like`, 400); the reply is redacted too. `lint` (prescriptive wording) and `ungrounded` (numbers not in `digest`, when the
        caller has the desk's digest text) are soft flags: they never block or rewrite a reply."""
        http = llm_config.route() == "http"
        if self.cfg is None and not http:
            raise ChatError("chat off", 503)
        pics = clean_images(images)
        if http and pics:
            raise ChatError("images need the desk agent", 400)   # the HTTP backend is text only
        if prompt is None and pics:
            prompt = ""
        if not isinstance(prompt, str) or not (prompt.strip() or pics):
            raise ChatError("empty prompt", 400)
        if not prompt.strip():
            prompt = "Look at the attached screenshot."
        if len(prompt) > MAX_PROMPT:
            raise ChatError("prompt too long", 400)
        if redact.key_like(prompt):
            raise ChatError("key_like", 400)
        prompt = redact.redact_secrets(prompt)
        if self.lock.locked():
            raise ChatError("busy", 429)           # the agent runs one question at a time
        if http:
            return await self._ask_http(prompt.strip(), digest)
        async with self.lock:
            url, token = self.cfg
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_S) as c:
                    r = await c.post(url + "/ask", json={"prompt": prompt.strip(), "session": SESSION, "images": pics},
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
            reply = redact.redact_secrets(reply[:MAX_REPLY])
            return {"reply": reply, "lint": lint.lint_reply(reply),
                    "ungrounded": ground.ground_reply(reply, digest) if isinstance(digest, str) and digest else []}

    async def _ask_http(self, prompt, digest):
        """The text-only provider path (llm_chat): fixed desk rules plus the digest as the only context, no tools, one turn, same soft flags plus `guard` (reply_guard words). A reply that is only tool-call
        markup is `bad_reply`. The claude -p desk-agent path above is NOT guarded by reply_guard."""
        cfg = llm_config.resolve()
        if cfg is None:
            raise ChatError("not_configured", 400)
        if self.lock.locked():
            raise ChatError("busy", 429)
        async with self.lock:
            try:
                r = await llm_chat.ask(cfg, llm_chat.build_system(digest if isinstance(digest, str) else ""),
                                       [{"role": "user", "content": prompt}], transport=self.transport)
            except llm_chat.LlmError as e:
                raise ChatError(e.code, 429 if e.code == "rate_limited" else 502)
        reply = redact.redact_secrets(r.text[:MAX_REPLY])
        if reply_guard.only_markup(reply):
            raise ChatError("bad_reply", 502)
        return {"reply": reply, "lint": lint.lint_reply(reply), "guard": reply_guard.check(reply),
                "ungrounded": ground.ground_reply(reply, digest) if isinstance(digest, str) and digest else [],
                "usage": r.usage()}
