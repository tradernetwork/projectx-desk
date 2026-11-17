"""OAuth 2.1 for the Robinhood MCP: discovery, dynamic client registration, PKCE (S256), a one-shot loopback
callback listener, token exchange and refresh. Nothing here logs; errors are fixed `McpError` codes.

Flow and payload shapes follow tradermatrix-desktop `src-tauri/src/rh/oauth.rs` (read for reference only). The
metadata URL and the 404 fallback endpoints are UNVERIFIED against the live service; the capture in phase 0 settles it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
import urllib.parse
from urllib.parse import urlparse

import httpx

from .mcp import McpError
from .tokens import Tokens

LOGIN_EXPIRY_S = 600.0
DEFAULT_EXPIRES_IN = 24 * 3600
SCOPE = "internal"
CLIENT_NAME = "ProjectX Desk"
CALLBACK_HOSTS = ("localhost", "127.0.0.1")
FALLBACK_HOST = "agent.robinhood.com"      # prior-art endpoints, only for the real host and only on a 404


def _loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")


def _rh_host(host):
    return bool(host) and (host == "robinhood.com" or host.endswith(".robinhood.com"))


def allowed_auth_url(url: str, base_url: str) -> bool:
    """True when `url` may be handed to a browser or fetched as an OAuth endpoint for `base_url`.

    Robinhood: https on robinhood.com or a subdomain. Anything else: https on the configured host. A loopback base
    (the mock) allows only the same loopback origin over http.
    """
    try:
        u, b = urlparse(url), urlparse(base_url)
    except ValueError:
        return False
    if _loopback(b.hostname):
        return u.scheme == "http" and u.hostname == b.hostname and u.port == b.port
    if u.scheme != "https" or not u.hostname:
        return False
    return _rh_host(u.hostname) if _rh_host(b.hostname) else u.hostname == b.hostname


def _client(transport):
    return httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=httpx.Timeout(30.0))


def _origin(base_url):
    u = urlparse(base_url)
    return f"{u.scheme}://{u.netloc}"


def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _send(http, method, url, **kw):
    try:
        r = await http.request(method, url, **kw)
    except httpx.TimeoutException:
        raise McpError("timeout") from None
    except httpx.HTTPError:
        raise McpError("unreachable") from None
    if 300 <= r.status_code < 400:
        raise McpError("redirect")
    return r


async def discover(http, base_url):
    """-> dict with authorization_endpoint, token_endpoint, registration_endpoint, issuer, iss_required."""
    r = await _send(http, "GET", _origin(base_url) + "/.well-known/oauth-authorization-server",
                    headers={"Accept": "application/json"})
    if r.status_code == 404 and urlparse(base_url).hostname == FALLBACK_HOST:
        return {"issuer": None, "iss_required": False,
                "authorization_endpoint": "https://robinhood.com/oauth",
                "token_endpoint": "https://api.robinhood.com/oauth2/token/",
                "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register"}
    if not 200 <= r.status_code < 300:
        raise McpError("bad_reply")
    try:
        d = r.json()
        meta = {k: d[k] for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")}
    except (ValueError, KeyError, TypeError):
        raise McpError("bad_reply") from None
    for v in meta.values():
        if not isinstance(v, str) or not allowed_auth_url(v, base_url):
            raise McpError("bad_reply")
    iss = d.get("issuer")
    meta["issuer"] = iss if isinstance(iss, str) else None
    meta["iss_required"] = d.get("authorization_response_iss_parameter_supported") is True
    return meta


def _tokens_from(r, client_id, now, keep_refresh=None) -> Tokens:
    try:
        d = r.json()
        access = d["access_token"]
        refresh = d.get("refresh_token") or keep_refresh
        exp = d.get("expires_in", DEFAULT_EXPIRES_IN)
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
            raise ValueError
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not 0 < exp < 10 * 365 * 86400:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise McpError("bad_reply") from None
    return Tokens(access, refresh, now() + float(exp), client_id)


async def refresh_tokens(base_url, tokens: Tokens, transport=None, now=time.time) -> Tokens:
    async with _client(transport) as http:
        meta = await discover(http, base_url)
        r = await _send(http, "POST", meta["token_endpoint"], headers={"Accept": "application/json"},
                        data={"grant_type": "refresh_token", "refresh_token": tokens.refresh,
                              "client_id": tokens.client_id})
        if r.status_code in (400, 401):
            raise McpError("unauthorized")
        if not 200 <= r.status_code < 300:
            raise McpError("bad_reply")
        return _tokens_from(r, tokens.client_id, now, keep_refresh=None)


class Login:
    def __init__(self, base_url, store, callback_port=None, opener=None, transport=None, now=time.time,
                 expiry_s=LOGIN_EXPIRY_S):
        self.base_url, self.store, self.opener, self.transport = base_url, store, opener, transport
        self.now, self.expiry_s = now, expiry_s
        if callback_port is None:
            raw = os.environ.get("PX_RH_CALLBACK_PORT", "").strip()
            callback_port = int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else 0
        self.callback_port = callback_port
        self._logins: dict[str, dict] = {}
        self._starting = False

    def _active(self):
        for pid, c in self._logins.items():
            if c["status"] == "pending" and self.now() <= c["deadline"]:
                return pid
        return None

    def status(self, pending_id) -> str:
        c = self._logins.get(pending_id)
        if c is None:
            return "failed"
        if c["status"] == "pending" and self.now() > c["deadline"]:
            return "expired"
        return c["status"]

    async def aclose(self):
        for c in self._logins.values():
            t = c.get("task")
            if t and not t.done():
                t.cancel()
            if c.get("server"):
                c["server"].close()
        for c in self._logins.values():
            t = c.get("task")
            if t:
                await asyncio.gather(t, return_exceptions=True)

    async def start(self) -> dict:
        if self._starting or self._active():
            raise McpError("rate_limited")
        self._starting = True
        ctx = {"status": "pending", "deadline": self.now() + self.expiry_s, "server": None, "task": None,
               "state": secrets.token_urlsafe(24), "future": asyncio.get_running_loop().create_future()}
        pid = secrets.token_urlsafe(8)
        try:
            async with _client(self.transport) as http:
                meta = await discover(http, self.base_url)
                ctx["server"] = await asyncio.start_server(
                    lambda r, w: self._on_connection(ctx, r, w), "127.0.0.1", self.callback_port)
                port = ctx["server"].sockets[0].getsockname()[1]
                redirect = f"http://localhost:{port}/callback"
                reg = await _send(http, "POST", meta["registration_endpoint"], headers={"Accept": "application/json"},
                                  json={"client_name": CLIENT_NAME, "redirect_uris": [redirect],
                                        "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"], "token_endpoint_auth_method": "none",
                                        "application_type": "native", "scope": SCOPE})
                try:
                    client_id = reg.json()["client_id"]
                except (ValueError, KeyError, TypeError):
                    client_id = None
                if not 200 <= reg.status_code < 300 or not isinstance(client_id, str) or not client_id:
                    raise McpError("bad_reply")
            verifier, challenge = pkce_pair()
            q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": SCOPE,
                 "state": ctx["state"], "code_challenge": challenge, "code_challenge_method": "S256"}
            url = meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?") \
                + urllib.parse.urlencode(q)
            if not allowed_auth_url(url, self.base_url):
                raise McpError("bad_reply")
            ctx.update(meta=meta, client_id=client_id, redirect=redirect, verifier=verifier)
            self._logins[pid] = ctx
            ctx["task"] = asyncio.create_task(self._finish(ctx))
            if self.opener is not None:
                try:
                    self.opener(url)
                except Exception:
                    raise McpError("unreachable") from None
            return {"url": url, "pending_id": pid}
        except BaseException:
            ctx["status"] = "failed"
            if ctx.get("task"):
                ctx["task"].cancel()
            if ctx["server"]:
                ctx["server"].close()
            self._logins.pop(pid, None)
            raise
        finally:
            self._starting = False

    # ---- loopback listener -----------------------------------------------------------------------------------
    async def _on_connection(self, ctx, reader, writer):
        code, status, body = None, "404 Not Found", b"not found"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            host = next((ln.split(":", 1)[1].strip() for ln in lines[1:] if ln.lower().startswith("host:")), "")
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            u = urlparse(parts[1]) if len(parts) >= 2 else None
            if parts[0] == "GET" and u is not None and u.path == "/callback" and host in CALLBACK_HOSTS:
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                ok = hmac.compare_digest(q.get("state", ""), ctx["state"]) and "error" not in q and bool(q.get("code"))
                iss_ok = (not ctx.get("meta", {}).get("iss_required")) or q.get("iss") == ctx["meta"].get("issuer")
                if ok and iss_ok:
                    code, status, body = q["code"], "200 OK", b"Signed in. You can close this tab."
                else:
                    status, body = "400 Bad Request", b"Sign-in failed. Start again from the desk."
                if not ctx["future"].done():
                    ctx["future"].set_result(code)
        except Exception:
            pass
        try:
            writer.writelines([f"HTTP/1.1 {status}\r\n".encode(), b"Content-Type: text/plain; charset=utf-8\r\n",
                               f"Content-Length: {len(body)}\r\n".encode(), b"Connection: close\r\n\r\n", body])
            await writer.drain()
            writer.close()
        except Exception:
            pass

    async def _finish(self, ctx):
        try:
            try:
                code = await asyncio.wait_for(ctx["future"], self.expiry_s)
            except asyncio.TimeoutError:
                ctx["status"] = "expired"
                return
            if code is None:
                ctx["status"] = "failed"
                return
            async with _client(self.transport) as http:
                r = await _send(http, "POST", ctx["meta"]["token_endpoint"], headers={"Accept": "application/json"},
                                data={"grant_type": "authorization_code", "code": code, "redirect_uri": ctx["redirect"],
                                      "client_id": ctx["client_id"], "code_verifier": ctx["verifier"]})
                if not 200 <= r.status_code < 300:
                    raise McpError("bad_reply")
                tokens = _tokens_from(r, ctx["client_id"], self.now)
            self.store.save(tokens)
            ctx["status"] = "done"
        except asyncio.CancelledError:
            ctx["status"] = "failed" if ctx["status"] == "pending" else ctx["status"]
            raise
        except Exception:
            ctx["status"] = "failed"
        finally:
            if ctx["server"]:
                ctx["server"].close()
