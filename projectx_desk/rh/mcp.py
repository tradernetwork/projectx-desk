"""Read-only MCP client for the Robinhood Trading MCP (Streamable HTTP, JSON-RPC 2.0).

Phase 0 has NO write path: `call` refuses any tool outside `READ_TOOLS` before a single byte is sent. Errors are
fixed codes only, never provider text. Redirects are never followed (the bearer token goes to the configured endpoint
and nowhere else), bodies are capped, calls are limited to 4 per second, and a read is retried once (after a token
refresh on 401, or once on a transport failure).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from urllib.parse import urlparse

import httpx

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
PROTOCOL_VERSION = "2025-06-18"
MAX_BODY = 1024 * 1024
CALLS_PER_S = 4
TIMEOUT_S = 30.0
CODES = ("unauthorized", "rate_limited", "unreachable", "timeout", "bad_reply", "too_big", "redirect", "write_blocked", "tool_error")

READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders", "get_equity_quotes",
    "get_option_positions", "get_option_orders", "get_option_chains", "get_realized_pnl",
    "get_trade_approvals", "get_trade_approval_setting",
    "get_equity_historicals",                                  # market data for the LETF desk charts; no account argument
})


class McpError(Exception):
    """`.code` is one of CODES. The message is the code: nothing from the provider ever rides along."""

    sent = False          # True only when a WRITE may have reached the server (see Client.submit); reads never set it

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _loopback(host: str | None) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


class _WriteCap:
    """Capability to send writes: it carries the exact tool names its holder may call. Only `mint_write_cap` makes one."""
    __slots__ = ("tools",)

    def __init__(self, tools, _token):
        if _token is not _MINT:
            raise McpError("write_blocked")
        self.tools = frozenset(tools)


_MINT = object()
_GUARD_MODULE = "projectx_desk.rh.guard"


def mint_write_cap(tools) -> _WriteCap:
    """Hand out the write capability, but only to the Robinhood guard module (checked from the caller's frame). Phase 0 and every
    other module hold no way to name a write tool here: the names come from the guard, and `submit` refuses without the key."""
    caller = sys._getframe(1).f_globals.get("__name__")
    if caller != _GUARD_MODULE:
        raise McpError("write_blocked")
    return _WriteCap(tools, _MINT)


class Limiter:
    """At most `n` calls in any rolling 1 s window. Clock and sleep are injectable."""

    def __init__(self, n=CALLS_PER_S, clock=time.monotonic, sleep=asyncio.sleep):
        self.n, self.clock, self.sleep = n, clock, sleep
        self._stamps: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = self.clock()
                while self._stamps and now - self._stamps[0] >= 1.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.n:
                    self._stamps.append(now)
                    return
                await self.sleep(max(self._stamps[0] + 1.0 - now, 0.001))


class Client:
    def __init__(self, base_url: str = ENDPOINT, token_provider=None, transport=None,
                 clock=time.monotonic, sleep=asyncio.sleep):
        u = urlparse(base_url)
        if not (u.scheme == "https" or (u.scheme == "http" and _loopback(u.hostname))):
            raise McpError("bad_reply")
        self.base_url = base_url
        self._tokens = token_provider
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                       timeout=httpx.Timeout(TIMEOUT_S))
        self._limiter = Limiter(clock=clock, sleep=sleep)
        self._sleep = sleep
        self._session: str | None = None
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._id = 0

    async def aclose(self):
        await self._client.aclose()

    # ---- transport -------------------------------------------------------------------------------------------
    async def _bearer(self, force=False) -> str:
        if self._tokens is None:
            raise McpError("unauthorized")
        try:
            if force:
                fn = getattr(self._tokens, "force_refresh", None)
                if fn is None:
                    raise McpError("unauthorized")
                return await fn()
            return await self._tokens.access_token()
        except McpError:
            raise
        except Exception:
            raise McpError("unauthorized") from None

    async def _post_once(self, body: dict, bearer: str):
        """One POST. Returns (status, headers, bytes). Streams so an oversized body is cut off, not buffered."""
        headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            async with self._client.stream("POST", self.base_url, content=json.dumps(body), headers=headers) as r:
                chunks, size = [], 0
                async for part in r.aiter_bytes():
                    size += len(part)
                    if size > MAX_BODY:
                        raise McpError("too_big")
                    chunks.append(part)
                return r.status_code, r.headers, b"".join(chunks)
        except McpError:
            raise
        except httpx.TimeoutException:
            raise McpError("timeout") from None
        except httpx.HTTPError:
            raise McpError("unreachable") from None

    async def _send(self, body: dict, expect_id=None):
        """POST with the limiter, one 401 refresh and one transport retry. Returns the JSON-RPC message or None."""
        refreshed = False
        retried = False
        bearer = await self._bearer()
        while True:
            await self._limiter.acquire()
            try:
                status, headers, raw = await self._post_once(body, bearer)
            except McpError as e:
                if e.code in ("unreachable", "timeout") and not retried:
                    retried = True
                    continue
                raise
            sid = headers.get("mcp-session-id")
            if sid:
                self._session = sid
            if 300 <= status < 400:
                raise McpError("redirect")
            if status == 401:
                if refreshed:
                    raise McpError("unauthorized")
                refreshed = True
                bearer = await self._bearer(force=True)
                continue
            if status == 429:
                raise McpError("rate_limited")
            if status >= 500:
                if not retried:
                    retried = True
                    continue
                raise McpError("unreachable")
            if status >= 400:
                raise McpError("bad_reply")
            if expect_id is None:
                return None
            return self._parse(raw, headers.get("content-type", ""), expect_id)

    @staticmethod
    def _parse(raw: bytes, ctype: str, expect_id):
        try:
            text = raw.decode("utf-8")
            if "text/event-stream" in ctype.lower():
                msgs = []
                for line in text.splitlines():
                    if line.startswith("data:"):
                        try:
                            msgs.append(json.loads(line[5:].strip()))
                        except ValueError:
                            continue
            else:
                msgs = [json.loads(text)]
        except (ValueError, UnicodeDecodeError):
            raise McpError("bad_reply") from None
        for m in msgs:
            if isinstance(m, dict) and m.get("jsonrpc") == "2.0" and m.get("id") == expect_id:
                return m
        raise McpError("bad_reply")

    async def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        msg = await self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, self._id)
        err = msg.get("error")
        if err is not None:
            if "RATE_LIMITED" in str(err):
                raise McpError("rate_limited")
            raise McpError("bad_reply")
        res = msg.get("result")
        if not isinstance(res, dict):
            raise McpError("bad_reply")
        return res

    async def _initialize(self):
        async with self._init_lock:
            if self._ready:
                return
            self._session = None
            await self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "projectx-desk", "version": "0"}})
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._ready = True

    # ---- public ----------------------------------------------------------------------------------------------
    async def list_tools(self) -> list[dict]:
        await self._initialize()
        out, cursor = [], None
        for _ in range(50):
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools = res.get("tools")
            if not isinstance(tools, list):
                raise McpError("bad_reply")
            out.extend(t for t in tools if isinstance(t, dict))
            cursor = res.get("nextCursor")
            if not cursor:
                return out
        raise McpError("bad_reply")

    async def call(self, tool: str, args: dict | None = None) -> dict:
        if tool not in READ_TOOLS:                    # before the limiter, the token and the network
            raise McpError("write_blocked")
        await self._initialize()
        res = await self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        if res.get("isError"):
            text = " ".join(p.get("text", "") for p in (res.get("content") or []) if isinstance(p, dict))
            raise McpError("rate_limited" if "RATE_LIMITED" in text else "bad_reply")
        return self._unwrap(res)

    @staticmethod
    def _unwrap(res: dict) -> dict:
        data = res.get("structuredContent")
        if not isinstance(data, (dict, list)):
            data = None
            for p in res.get("content") or []:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    try:
                        data = json.loads(p["text"])
                    except ValueError:
                        data = None
                    break
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        if isinstance(data, list):
            data = {"items": data}
        if not isinstance(data, dict):
            raise McpError("bad_reply")
        return data

    async def submit(self, tool: str, args: dict, *, _guard_key) -> dict:
        """THE write path, for `rh/guard.py` only. ONE attempt: no retry, no token refresh and resend, no limiter wait after the
        send. Without the key (or for a read tool, or a tool the key does not list) it raises `write_blocked` before any byte
        is sent. `McpError.sent` tells the guard whether the order MAY have been received: False means the server clearly did
        not act (an HTTP 4xx, a JSON-RPC or tool error reply, or a failure before the POST); True means unknown (transport,
        timeout, 5xx, redirect, oversized or unreadable reply)."""
        if not isinstance(_guard_key, _WriteCap) or tool not in _guard_key.tools or tool in READ_TOOLS:
            raise McpError("write_blocked")
        await self._initialize()
        bearer = await self._bearer()
        await self._limiter.acquire()
        self._id += 1
        rid = self._id
        body = {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}
        try:
            status, headers, raw = await self._post_once(body, bearer)
        except McpError as e:
            e.sent = True
            raise
        sid = headers.get("mcp-session-id")
        if sid:
            self._session = sid
        if 300 <= status < 400:
            e = McpError("redirect")
            e.sent = True
            raise e
        if status >= 500:
            e = McpError("unreachable")
            e.sent = True
            raise e
        if status >= 400:                              # 401, 429 and other 4xx: the server answered and did not act
            raise McpError({401: "unauthorized", 429: "rate_limited"}.get(status, "bad_reply"))
        try:
            msg = self._parse(raw, headers.get("content-type", ""), rid)
        except McpError as e:
            e.sent = True
            raise
        if msg.get("error") is not None:
            raise McpError("bad_reply")                # a JSON-RPC error: the call was refused as a request
        res = msg.get("result")
        if not isinstance(res, dict):
            e = McpError("bad_reply")
            e.sent = True
            raise e
        if res.get("isError"):
            raise McpError("tool_error")               # the tool answered with an error: nothing was created
        try:
            return self._unwrap(res)
        except McpError as e:
            e.sent = True
            raise
