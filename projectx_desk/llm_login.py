"""'Sign in with OpenRouter': OAuth PKCE to a loopback callback (port of TraderMatrix Desktop's openrouter_login.rs, Apache-2.0).

Facts (docs 2026-11-11): auth URL `https://openrouter.ai/auth` with `callback_url`, `code_challenge`, `code_challenge_method=S256`, `key_label`
and `state` (returned unchanged on the callback, not returned when the user denies); localhost callbacks work on any port (127.0.0.1 is not
documented, so the callback host is `localhost`); the code is single-use, 10 minutes; `POST https://openrouter.ai/api/v1/auth/keys` with
`{code, code_verifier, code_challenge_method}` returns `{key}`.

The browser gets a link and an opaque pending id to poll. It never sees the code, the verifier or the key. The minted key goes through
`llm_config.save` exactly like a pasted key (sealed to the OpenRouter URL) and is never returned or logged. Only https openrouter.ai URLs
are ever handed out to open. One pending login at a time.

HEADLESS BOXES: the callback lands on `localhost` of the machine whose browser the user signs in with. On a server with no browser
(the venus desk) it cannot reach the user's browser, so the sign-in cannot finish there. Paste a key instead (`POST /api/llm/config`
with `key`, or `PX_LLM_KEY`). Never talks to the guard, the client or the hubs.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import llm_config
from . import llm_providers as lp

KEY_LABEL = "ProjectX Desk"
LOGIN_EXPIRY_S = 600.0
EXCHANGE_TIMEOUT_S = 20.0
MAX_EXCHANGE = 16 * 1024
MAX_REQUEST_LINE = 4096

PENDING, DONE, EXPIRED, FAILED = "pending", "done", "expired", "failed"


@dataclass(frozen=True)
class LoginCfg:
    auth_url: str = "https://openrouter.ai/auth"
    exchange_url: str = "https://openrouter.ai/api/v1/auth/keys"
    expiry_s: float = LOGIN_EXPIRY_S
    callback_host: str = "localhost"


class LoginError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def is_allowed_openrouter_url(url):
    try:
        u = urlsplit(url)
        return u.scheme == "https" and u.hostname == "openrouter.ai" and u.username is None
    except ValueError:
        return False


def pkce_pair():
    """(verifier, S256 challenge), both base64url without padding. 32 random bytes give a 43-character verifier."""
    v = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


_PAGE = ("<!doctype html><meta charset=utf-8><title>ProjectX Desk</title>"
         "<body style=\"font-family:sans-serif;padding:2em\"><p>%s</p></body>")


def _http(status, text):
    body = (_PAGE % text).encode()
    head = f"HTTP/1.1 {status}\r\ncontent-type: text/html; charset=utf-8\r\ncontent-length: {len(body)}\r\nconnection: close\r\ncache-control: no-store\r\n\r\n"
    return head.encode() + body


class OrLogin:
    def __init__(self, cfg=None, config_path=None, environ=None, transport=None, opener=None):
        self.cfg = cfg or LoginCfg()
        self._config_path, self._environ, self._transport, self._opener = config_path, environ, transport, opener
        self._cur = None            # dict: id, status, started, task, server
        self._starting = False

    def status(self, pending_id):
        c = self._cur
        if c is None or not isinstance(pending_id, str) or not hmac.compare_digest(c["id"], pending_id):
            return FAILED
        if c["status"] == PENDING and time.monotonic() - c["started"] >= self.cfg.expiry_s:
            return EXPIRED
        return c["status"]

    def _live(self):
        c = self._cur
        return c is not None and c["status"] == PENDING and time.monotonic() - c["started"] < self.cfg.expiry_s

    async def start(self):
        """Bind a loopback port, build the sign-in link, try to open it, wait for the redirect in the background.
        Returns {"url", "pending_id"}. A second start while one is pending raises LoginError("rate_limited")."""
        if self._live() or self._starting:
            raise LoginError("rate_limited")
        self._starting = True
        try:
            verifier, challenge = pkce_pair()
            state = secrets.token_urlsafe(24)
            pid = secrets.token_hex(16)
            done = asyncio.Event()
            result = {}

            async def handle(reader, writer):
                try:
                    await self._handle(reader, writer, state, result, done)
                except Exception:   # a malformed probe must never take the listener down
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            try:
                server = await asyncio.start_server(handle, "127.0.0.1", 0)
            except OSError:
                raise LoginError("unreachable") from None
            port = server.sockets[0].getsockname()[1]
            callback = f"http://{self.cfg.callback_host}:{port}/callback"
            auth = self.cfg.auth_url + "?" + urlencode({"callback_url": callback, "code_challenge": challenge,
                                                        "code_challenge_method": "S256", "key_label": KEY_LABEL, "state": state})
            cur = {"id": pid, "status": PENDING, "started": time.monotonic(), "server": server, "task": None}
            self._cur = cur
            cur["task"] = asyncio.create_task(self._run(cur, server, verifier, result, done))
            if self._opener is not None and is_allowed_openrouter_url(auth):
                try:
                    self._opener(auth)
                except Exception:
                    pass
            return {"url": auth, "pending_id": pid}
        finally:
            self._starting = False

    async def _handle(self, reader, writer, state, result, done):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5.0)
        if len(raw) > MAX_REQUEST_LINE:
            return
        parts = raw.decode("latin-1").split(" ")
        if len(parts) < 2 or parts[0] != "GET":
            writer.writelines([_http("405 Method Not Allowed", "Not allowed.")])
            return
        u = urlsplit(parts[1])
        if u.path != "/callback":
            writer.writelines([_http("404 Not Found", "Not found.")])
            return
        q = parse_qs(u.query)
        got = (q.get("state") or [""])[0]
        if got:
            if not hmac.compare_digest(got.encode(), state.encode()):
                writer.writelines([_http("400 Bad Request", "That sign-in link does not belong to this window.")])
                return
        elif "error" in q or "code" not in q:
            result["denied"] = True        # the user denied: OpenRouter sends no state
            writer.writelines([_http("200 OK", "Sign-in cancelled. You can close this tab.")])
            done.set()
            return
        else:
            writer.writelines([_http("400 Bad Request", "Missing state.")])
            return
        code = (q.get("code") or [""])[0]
        if not code or len(code) > 512:
            writer.writelines([_http("400 Bad Request", "Missing code.")])
            return
        result["code"] = code
        writer.writelines([_http("200 OK", "Signed in. You can close this tab and return to the desk.")])
        await writer.drain()
        done.set()

    async def _run(self, cur, server, verifier, result, done):
        status = FAILED
        try:
            try:
                await asyncio.wait_for(done.wait(), self.cfg.expiry_s)
            except asyncio.TimeoutError:
                status = EXPIRED
                return
            finally:
                server.close()          # port freed as soon as the redirect (or the expiry) happens
            code = result.get("code")
            if not code:
                return
            key = await self._exchange(code, verifier)
            if key and self._save(key):
                status = DONE
        except Exception:
            status = FAILED
        finally:
            cur["status"] = status
            result.clear()

    async def _exchange(self, code, verifier):
        body = {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
        try:
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=False, timeout=EXCHANGE_TIMEOUT_S, trust_env=False) as c:
                async with c.stream("POST", self.cfg.exchange_url, json=body) as r:
                    if r.status_code != 200:
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_EXCHANGE:
                            return None
            key = json.loads(bytes(buf)).get("key")
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        if not isinstance(key, str):
            return None
        key = key.strip()
        return key if lp.key_shape_error(lp.preset("openrouter"), key) is None else None

    def _save(self, key):
        try:
            old = llm_config.load(self._config_path, self._environ)
            keep = old is not None and old.provider == "openrouter"
            llm_config.save({"provider": "openrouter", "key": key, "model": old.model if keep else ""},
                            self._config_path, self._environ)
            return True
        except llm_config.ConfigError:
            return False

    async def cancel(self):
        c = self._cur
        if c and c["task"] and not c["task"].done():
            c["task"].cancel()
            try:
                await c["task"]
            except BaseException:
                pass
        if c:
            c["server"].close()
            if c["status"] == PENDING:
                c["status"] = FAILED
