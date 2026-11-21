"""The one I/O of the Public tab (Round 36): a GET of the sidecar's /ai/state over loopback http, and nothing else.

Rules, each test pinned:
  * PX_PUB_URL unset or blank: `from_env` returns None and the feature is absent (no route, no tab, no digest line).
  * The URL must be plain http to a loopback host (127.0.0.0/8, ::1, localhost) with no userinfo, query, fragment or path.
    Anything else is the fixed code `not_allowed` and nothing is ever fetched. The URL itself is never logged.
  * GET only. No redirect is followed (a 3xx is `bad_status`), proxy variables are ignored, the body is capped at 256 KB
    while it streams in (`too_big`), and the whole fetch is bounded by a 3 s timeout (`timeout`), so a hung sidecar costs
    at most 3 s of one awaiting coroutine and never blocks the event loop.
  * One result per stream flag is cached for about 5 s, single flight: any number of callers share one request. A failure
    is cached the same way (a down sidecar is not hammered) and is never covered with stale data: every section is None.
  * Every error is one of CODES. Sidecar text never leaves this module: the JSON is reduced by `view.reduce`.
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

CODES = ("unset", "unreachable", "timeout", "bad_status", "too_big", "bad_json", "not_allowed")
TIMEOUT_S = 3.0
MAX_BODY = 256 * 1024
TTL_S = 5.0
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
        self._cache: Dict[bool, tuple] = {}          # stream -> (attempted_at, fetched_at or None, view)
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
        if hit is None or self._clock() - hit[0] >= TTL_S:
            return None
        return self._aged(hit)

    def _aged(self, hit: tuple) -> Dict[str, Any]:
        view = dict(hit[2])
        view["age_s"] = None if hit[1] is None else max(0, int(self._clock() - hit[1]))
        return view

    async def state(self, stream: bool = False) -> Dict[str, Any]:
        """The reduced view for this stream flag, from the cache when it is under TTL_S old. Never raises."""
        stream = bool(stream)
        if self.base is None:
            return pub_view.failed(self.code or "not_allowed", stream)
        hit = self._fresh(stream)
        if hit is not None:
            return hit
        async with self._lock(stream):
            hit = self._fresh(stream)               # another caller fetched while this one waited: share it
            if hit is not None:
                return hit
            started = self._clock()
            got = await self._get(stream)
            if isinstance(got, _Fail):
                view, fetched = pub_view.failed(got.code, stream), None
            else:
                view = pub_view.reduce(got, stream)
                if view is None:
                    view, fetched = pub_view.failed("bad_json", stream), None
                else:
                    fetched = self._clock()
            self._cache[stream] = (started, fetched, view)
            self.last = {"reachable": view["reachable"], "code": view["code"], "fetched_at": fetched}
            return self._aged(self._cache[stream])

    def health(self) -> Dict[str, Any]:
        """For /state.json: what the last attempt found. No fetch, never raises."""
        f = self.last.get("fetched_at")
        return {"enabled": True, "reachable": self.last.get("reachable"), "code": self.last.get("code"),
                "age_s": None if f is None else max(0, int(self._clock() - f))}

    async def _get(self, stream: bool) -> Any:
        url = self.base + PATH + ("?stream=1" if stream else "?stream=0")
        try:
            return await asyncio.wait_for(self._fetch(url), self._timeout)
        except asyncio.TimeoutError:
            return _Fail("timeout")

    async def _fetch(self, url: str) -> Any:
        self.requests += 1
        buf = bytearray()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), follow_redirects=False, trust_env=False,
                                         transport=self._transport) as c:
                async with c.stream("GET", url, headers={"accept": "application/json"}) as r:
                    if r.status_code != 200:
                        return _Fail("bad_status")
                    cl = r.headers.get("content-length")
                    if cl is not None and cl.strip().isdigit() and int(cl.strip()) > MAX_BODY:
                        return _Fail("too_big")
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > MAX_BODY:
                            return _Fail("too_big")
        except httpx.TimeoutException:
            return _Fail("timeout")
        except (httpx.HTTPError, OSError, ValueError):
            return _Fail("unreachable")
        try:
            return decode(bytes(buf))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return _Fail("bad_json")


def from_env(env: Optional[Dict[str, str]] = None, transport: Any = None) -> Optional[PubLink]:
    """None when PX_PUB_URL is unset or blank (the feature is absent). A value that is not a loopback http URL gives a link
    that fetches nothing and reports `not_allowed`."""
    raw = (os.environ if env is None else env).get("PX_PUB_URL")
    if raw is None or not str(raw).strip():
        return None
    base = parse_url(raw)
    return PubLink(base, code=None if base else "not_allowed", transport=transport)
