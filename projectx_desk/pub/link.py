"""The one I/O of the Public tab (Round 36): a GET of the sidecar's /ai/state over loopback http, and nothing else.

Rules, each test pinned:
  * PX_PUB_URL unset or blank: `from_env` returns None and the feature is absent (no route, no tab, no digest line).
  * The URL must be plain http to a loopback host (127.0.0.0/8, ::1, localhost) with no userinfo, query, fragment or path.
    Anything else is the fixed code `not_allowed` and nothing is ever fetched. The URL itself is never logged.
  * GET only, `Accept-Encoding: identity` (a compressed reply is `bad_status`: no decompression bomb). No redirect is
    followed (a 3xx is `bad_status`), proxy variables are ignored, the raw body is capped at 256 KB while it streams in
    (`too_big`). The fetch AND the parse and reduction (run in a worker thread, every number bounded before it is
    formatted) sit inside one 3 s bound (`timeout`): a hung or hostile sidecar costs one awaiting coroutine, never the loop.
  * One result per stream flag is cached, single flight: any number of callers share one request. A success is fresh for
    TTL_S and a failure for FAIL_TTL_S, both counted from the END of the attempt; a failure is never covered with stale data
    (every section None). `peek` never waits: the digest, /ai/state and MCP read the cache and a refresh runs in the
    background; before the first attempt ends they read `pending`.
  * `state` and `peek` never raise. Every error is one of CODES. Sidecar text never leaves: the JSON is reduced by
    `view.reduce`.
"""
import asyncio
import ipaddress
import json
import os
import time
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

from projectx_desk.pub import view as pub_view

CODES = ("unset", "unreachable", "timeout", "bad_status", "too_big", "bad_json", "not_allowed", "pending")
TIMEOUT_S = 3.0
MAX_BODY = 256 * 1024
TTL_S = 5.0
FAIL_TTL_S = 10.0
PATH = "/ai/state"
MAX_URL = 200


def parse_url(raw: Any) -> Optional[str]:
    """The normalised base URL (`http://host:port`), or None when the value is not a plain loopback http URL."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > MAX_URL or any(ord(ch) < 33 or ord(ch) > 126 for ch in s):
        return None
    try:
        u = urlsplit(s)
        port = u.port                                   # raises on a garbled port
    except ValueError:
        return None
    if u.scheme != "http" or "@" in u.netloc or u.query or u.fragment or u.path not in ("", "/"):
        return None
    host = (u.hostname or "").lower()
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            return None
    if port is not None and not 0 < port < 65536:
        return None
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}" + (f":{port}" if port is not None else "")


def _no_constant(_name):
    raise ValueError("non-finite number")             # NaN, Infinity and -Infinity are not JSON numbers here


def decode(body: bytes) -> Any:
    """Parse with Decimal for every non-integer number: a float never exists. Raises ValueError on anything malformed."""
    return json.loads(body.decode("utf-8"), parse_float=Decimal, parse_constant=_no_constant)


def _parse(body: bytes, stream: bool) -> Any:
    """Decode and reduce, in a worker thread. The reduced view, or a _Fail. Never raises."""
    try:
        view = pub_view.reduce(decode(body), stream)
    except Exception:  # noqa: BLE001 - malformed, too deep, or anything else: a fixed code, never text
        return _Fail("bad_json")
    return _Fail("bad_json") if view is None else view


class _Fail:
    """A failed fetch, kept apart from any JSON value (a document that is itself a string is bad_json, not a code)."""
    __slots__ = ("code",)

    def __init__(self, code: str):
        self.code = code


class PubLink:
    """A read-only link. `base` is a parsed URL or None (then `code` says why and nothing is fetched)."""

    def __init__(self, base: Optional[str], code: Optional[str] = None, transport: Any = None,
                 clock=time.monotonic, timeout_s: float = TIMEOUT_S):
        self.base = base
        self.code = code if base is None else None
        self._transport = transport
        self._clock = clock
        self._timeout = float(timeout_s)
        self._cache: Dict[bool, tuple] = {}          # stream -> (ended_at, fetched_at or None, view)
        self._bg: Dict[bool, Any] = {}               # stream -> the background refresh task started by peek
        self._locks: Dict[bool, tuple] = {}          # stream -> (loop, lock): a lock is bound to the loop it first ran on
        self.requests = 0                            # GETs actually sent (tests read it)
        self.last: Dict[str, Any] = {"reachable": None, "code": self.code, "fetched_at": None}

    # ---- the read ---------------------------------------------------------------------------------------------------
    def _lock(self, stream: bool) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        hit = self._locks.get(stream)
        if hit is None or hit[0] is not loop:
            hit = (loop, asyncio.Lock())
            self._locks[stream] = hit
        return hit[1]

    def _fresh(self, stream: bool) -> Optional[Dict[str, Any]]:
        hit = self._cache.get(stream)
        if hit is None or self._clock() - hit[0] >= (TTL_S if hit[1] is not None else FAIL_TTL_S):
            return None
        return self._aged(hit)

    def _aged(self, hit: tuple) -> Dict[str, Any]:
        view = dict(hit[2])
        view["age_s"] = None if hit[1] is None else max(0, int(self._clock() - hit[1]))
        return view

    async def state(self, stream: bool = False) -> Dict[str, Any]:
        """The reduced view for this stream flag, from the cache while it is fresh, else one bounded fetch. Never raises."""
        stream = bool(stream)
        try:
            return await self._state(stream)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - documented never to raise: UNKNOWN with a fixed code
            return pub_view.failed("unreachable", stream)

    async def _state(self, stream: bool) -> Dict[str, Any]:
        if self.base is None:
            return pub_view.failed(self.code or "not_allowed", stream)
        hit = self._fresh(stream)
        if hit is not None:
            return hit
        async with self._lock(stream):
            hit = self._fresh(stream)               # another caller fetched while this one waited: share it
            if hit is not None:
                return hit
            got = await self._get(stream)
            ended = self._clock()
            if isinstance(got, _Fail):
                view, fetched = pub_view.failed(got.code, stream), None
            else:
                view, fetched = got, ended
            self._cache[stream] = (ended, fetched, view)
            self.last = {"reachable": view["reachable"], "code": view["code"], "fetched_at": fetched}
            return self._aged(self._cache[stream])

    def peek(self, stream: bool = False) -> Dict[str, Any]:
        """The last view for this flag WITHOUT waiting (the digest, /ai/state and MCP use it), with its age. A stale or missing
        entry starts one background refresh. `pending` before the first attempt ends. Never raises."""
        stream = bool(stream)
        try:
            if self.base is None:
                return pub_view.failed(self.code or "not_allowed", stream)
            if self._fresh(stream) is None:
                task = self._bg.get(stream)
                if task is None or task.done():
                    self._bg[stream] = asyncio.get_running_loop().create_task(self.state(stream))
            hit = self._cache.get(stream)
            return self._aged(hit) if hit is not None else pub_view.failed("pending", stream)
        except Exception:  # noqa: BLE001
            return pub_view.failed("unreachable", stream)

    def health(self) -> Dict[str, Any]:
        """For /state.json: what the last attempt found. No fetch, never raises."""
        f = self.last.get("fetched_at")
        return {"enabled": True, "reachable": self.last.get("reachable"), "code": self.last.get("code"),
                "age_s": None if f is None else max(0, int(self._clock() - f))}

    async def _get(self, stream: bool) -> Any:
        url = self.base + PATH + ("?stream=1" if stream else "?stream=0")
        try:
            return await asyncio.wait_for(self._fetch_and_parse(url, stream), self._timeout)
        except asyncio.TimeoutError:
            return _Fail("timeout")

    async def _fetch_and_parse(self, url: str, stream: bool) -> Any:
        body = await self._fetch(url)
        if isinstance(body, _Fail):
            return body
        return await asyncio.to_thread(_parse, body, stream)

    async def _fetch(self, url: str) -> Any:
        self.requests += 1
        buf = bytearray()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), follow_redirects=False, trust_env=False,
                                         transport=self._transport) as c:
                async with c.stream("GET", url, headers={"accept": "application/json", "accept-encoding": "identity"}) as r:
                    if r.status_code != 200:
                        return _Fail("bad_status")
                    enc = (r.headers.get("content-encoding") or "identity").strip().lower()
                    if enc != "identity":
                        return _Fail("bad_status")
                    cl = r.headers.get("content-length")
                    if cl is not None and cl.strip().isdigit() and int(cl.strip()) > MAX_BODY:
                        return _Fail("too_big")
                    async for chunk in r.aiter_bytes():         # identity only (checked above): no decompression here
                        buf += chunk
                        if len(buf) > MAX_BODY:
                            return _Fail("too_big")
        except httpx.TimeoutException:
            return _Fail("timeout")
        except (httpx.HTTPError, OSError, ValueError):
            return _Fail("unreachable")
        return bytes(buf)


def from_env(env: Optional[Dict[str, str]] = None, transport: Any = None) -> Optional[PubLink]:
    """None when PX_PUB_URL is unset or blank (the feature is absent). A value that is not a loopback http URL gives a link
    that fetches nothing and reports `not_allowed`."""
    raw = (os.environ if env is None else env).get("PX_PUB_URL")
    if raw is None or not str(raw).strip():
        return None
    base = parse_url(raw)
    return PubLink(base, code=None if base else "not_allowed", transport=transport)
