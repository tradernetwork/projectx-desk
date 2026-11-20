"""The leveraged-ETF desk's server side: the vendored universe, the chart ranges, the symbol allowlist, the response cache and
its call budget. Display only. Pure apart from one file read; imports no guard, client, hub, mcp or app (test pinned).

The routes in `app.py` are thin: they hand a reader (anything with async `bars` and `quotes`, see `rh/reads.py`) to the
`bars_response` and `quotes_response` builders below, which return plain dicts. A refusal is `{"ok": False, "code": <fixed word>}`
and nothing else: no provider text, no exception message.

The routes can only fetch symbols the vendored universe names (every fund plus every chart symbol), so they are a LETF desk and
not a general market-data proxy for whatever can reach loopback.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from .views import SYMBOL

DEFAULT_PATH = Path(__file__).with_name("letf_data.json")
ET = ZoneInfo("America/New_York")

GROUP_SLUGS = ("index", "sector", "single-stock", "commodity", "crypto", "bond", "country", "currency", "volatility")
KINDS = ("self", "proxy", "none")
_NAME = re.compile(r"[A-Za-z0-9 &.,'()/+%\-]{1,96}")
_ISSUER = re.compile(r"[A-Za-z0-9 &.\-]{1,40}")
_LABEL = re.compile(r"[A-Za-z0-9 &.,'()/+\-]{1,48}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# The codes a route may answer with. The first group is the desk's own; the second is `rh/mcp.py` `CODES` (a test keeps it equal).
CODES = ("bad_tf", "bad_symbol", "not_in_universe", "too_many", "not_connected", "busy", "letf_unavailable", "read_failed")
MCP_CODES = frozenset({"unauthorized", "rate_limited", "unreachable", "timeout", "bad_reply", "too_big", "redirect",
                       "write_blocked", "tool_error"})

MAX_BARS_PER_SYMBOL = 800        # the 1 MB reply cap bites before the broker's 5000-bar cap (about 370 bytes a bar, sent twice)
MAX_BARS_PER_CALL = 1600
MAX_SYMBOLS_BARS = 2
MAX_SYMBOLS_QUOTES = 4

# tf -> the broker interval, the size of one fetched bar, weekdays of history, expected bars per regular session, and how many
# fetched bars make one drawn bar (15m has no broker interval: three 5-minute bars are combined here).
TF = {
    "1m": {"interval": "minute", "fetch_ms": 60_000, "k": 1, "days": 2, "per_day": 390},
    "5m": {"interval": "5minute", "fetch_ms": 300_000, "k": 1, "days": 10, "per_day": 78},
    "15m": {"interval": "5minute", "fetch_ms": 300_000, "k": 3, "days": 20, "per_day": 78},
    "30m": {"interval": "30minute", "fetch_ms": 1_800_000, "k": 1, "days": 60, "per_day": 13},
    "1h": {"interval": "hour", "fetch_ms": 3_600_000, "k": 1, "days": 110, "per_day": 7},
    "1D": {"interval": "day", "fetch_ms": 86_400_000, "k": 1, "days": 500, "per_day": 1},
}
BARS_TTL_S = {"1m": 15.0, "5m": 30.0, "15m": 30.0, "30m": 120.0, "1h": 300.0, "1D": 1800.0}
QUOTES_TTL_S = 2.0
CACHE_MAX = 64
BUDGET_PER_S = 2.0               # market-data calls per second; the MCP limiter is 4 and the guard and the 30 s poll share it
BUDGET_BURST = 4.0


class LetfError(Exception):
    """`.code` is one of CODES (or an McpError code, carried through unchanged by the response builders)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- the vendored universe -----------------------------------------------------------------------------------------------------
class Universe:
    def __init__(self, doc):
        self.doc = doc
        self.allowed = frozenset(doc["funds"]) | frozenset(u["chart"] for u in doc["underlyings"] if u["chart"])

    def public_view(self, today=None):
        d = self.doc
        today = today or utcnow().date()
        age = (today - date.fromisoformat(d["as_of"])).days
        return {"ok": True, "as_of": d["as_of"], "age_days": max(0, age), "groups": d["groups"],
                "underlyings": d["underlyings"], "funds": d["funds"]}


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load(path=DEFAULT_PATH):
    """The universe, or None when the file is missing or fails ANY check (the route then answers `letf_unavailable`).
    Everything is re-validated here even though the sync script validated it: a hand edit must not reach the page."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("version") != 1 or not _DATE.fullmatch(doc.get("as_of", "")):
            return None
        date.fromisoformat(doc["as_of"])
        if [g[0] for g in doc["groups"]] != list(GROUP_SLUGS):
            return None
        funds = doc["funds"]
        if not isinstance(funds, dict) or not funds:
            return None
        for sym, f in funds.items():
            if not SYMBOL.fullmatch(sym) or not _finite(f["lev"]) or not 1.5 <= abs(f["lev"]) <= 5:
                return None
            if f["dv20"] is not None and not (_finite(f["dv20"]) and f["dv20"] >= 0):
                return None
            if f["name"] is not None and not _NAME.fullmatch(f["name"]):
                return None
            if f["issuer"] is not None and not _ISSUER.fullmatch(f["issuer"]):
                return None
            if not _LABEL.fullmatch(f["u"]):
                return None
        keys = set()
        for u in doc["underlyings"]:
            if not _LABEL.fullmatch(u["key"]) or u["key"] in keys or u["group"] not in GROUP_SLUGS or u["chart_kind"] not in KINDS:
                return None
            keys.add(u["key"])
            if u["chart"] is not None and not SYMBOL.fullmatch(u["chart"]):
                return None
            if (u["chart"] is None) != (u["chart_kind"] == "none"):
                return None
            if u["proxy_quality"] not in (None, "exact", "approximate"):
                return None
            for side in ("long", "short"):
                for s in u[side]:
                    if s not in funds or funds[s]["u"] != u["key"]:
                        return None
                if u[side + "_pick"] is not None and u[side + "_pick"] not in u[side]:
                    return None
        return Universe(doc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


# ---- chart ranges --------------------------------------------------------------------------------------------------------------
def est_bars(tf) -> int:
    t = TF[tf]
    return t["per_day"] * t["days"]


def range_for(tf, now=None):
    """-> (start as an RFC3339 string, expected fetched bars per symbol). `days` weekdays back from today, at 00:00 UTC."""
    now = now or utcnow()
    n, d = TF[tf]["days"], now.astimezone(timezone.utc).date()
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.strftime("%Y-%m-%dT00:00:00Z"), est_bars(tf)


def split_calls(symbols, tf):
    """One call for all symbols when the reply is expected to stay under the cap, else one call per symbol."""
    return [list(symbols)] if est_bars(tf) * len(symbols) <= MAX_BARS_PER_CALL else [[s] for s in symbols]


def parse_symbols(text, universe, max_n):
    """'qqq, TQQQ' -> ['QQQ', 'TQQQ'], or LetfError(bad_symbol | not_in_universe | too_many). Order kept, duplicates dropped."""
    if not isinstance(text, str) or not text or len(text) > 64:
        raise LetfError("bad_symbol")
    out = []
    for part in text.split(","):
        s = part.strip().upper()
        if not SYMBOL.fullmatch(s):
            raise LetfError("bad_symbol")
        if s not in out:
            out.append(s)
    if len(out) > max_n:
        raise LetfError("too_many")
    if any(s not in universe.allowed for s in out):
        raise LetfError("not_in_universe")
    return out


def aggregate(bars, k, bar_ms):
    """Combine k fetched bars into one drawn bar by flooring the start time to a multiple of k * bar_ms (the 09:30 ET open is a
    multiple of 15 minutes in both EST and EDT, so 5-minute bars line up). -> (rows, forming) where `forming` is True when the
    last bucket holds fewer than k bars. A bucket with any unknown volume has unknown volume (never a sum that understates)."""
    if k <= 1:
        return [list(b) for b in bars], False
    span = k * bar_ms
    out, count = [], []
    for t, o, h, l, c, v in bars:
        key = (t // span) * span
        if out and out[-1][0] == key:
            r = out[-1]
            r[2], r[3], r[4] = max(r[2], h), min(r[3], l), c
            r[5] = None if (r[5] is None or v is None) else r[5] + v
            count[-1] += 1
        else:
            out.append([key, o, h, l, c, v])
            count.append(1)
    return out, bool(count) and count[-1] < k


def equity_session_open(now) -> bool:
    """Regular US equity hours, Mon-Fri 09:30 to 16:00 ET. Holidays are not modelled. Kept equal to `rh/guard.market_open_now`
    by a test, because this module must not import the guard."""
    local = now.astimezone(ET)
    return local.weekday() < 5 and dtime(9, 30) <= local.time() < dtime(16, 0)


# ---- the cache and the call budget ---------------------------------------------------------------------------------------------
class MarketCache:
    """TTL cache, LRU-capped, with single flight per key and a token bucket for outbound market-data calls.

    Two tabs asking for the same thing make one call. When the bucket is empty a cache miss raises LetfError('busy') and the client
    retries on its next tick, so the charts can never use more than half of the 4 calls a second the Robinhood client allows (the
    guard's commit-point reads and the 30 s poll share the rest)."""

    def __init__(self, clock=time.monotonic, per_s=BUDGET_PER_S, burst=BUDGET_BURST, max_entries=CACHE_MAX):
        self.clock, self.per_s, self.burst, self.max = clock, per_s, burst, max_entries
        self._c: OrderedDict = OrderedDict()
        self._fl: dict = {}
        self._tokens, self._last = burst, clock()
        self.fetches = 0

    def _take(self, n) -> bool:
        now = self.clock()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.per_s)
        self._last = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    async def get(self, key, ttl, fetch, calls=1):
        """-> (value, cached). `fetch` is an async callable making `calls` outbound calls. A failure is never cached."""
        now = self.clock()
        hit = self._c.get(key)
        if hit is not None and hit[0] > now:
            self._c.move_to_end(key)
            return hit[1], True
        fut = self._fl.get(key)
        if fut is not None:
            return await fut, True
        if not self._take(calls):
            raise LetfError("busy")
        fut = asyncio.get_running_loop().create_future()
        self._fl[key] = fut
        try:
            self.fetches += 1
            val = await fetch()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as e:  # noqa: BLE001 - handed on to every waiter, then to the caller
            fut.set_exception(e)
            fut.exception()                        # mark retrieved: nobody may be waiting
            raise
        else:
            fut.set_result(val)
            self._c[key] = (self.clock() + ttl, val)
            self._c.move_to_end(key)
            while len(self._c) > self.max:
                self._c.popitem(last=False)
            return val, False
        finally:
            self._fl.pop(key, None)


# ---- response builders (what the routes return) --------------------------------------------------------------------------------
def fail(code) -> dict:
    return {"ok": False, "code": code}


def _code_of(e) -> str:
    """A fixed code for any failure: ours, an McpError's, or 'read_failed'. Never the message."""
    try:
        c = e.code
    except AttributeError:
        return "read_failed"
    return c if c in CODES or c in MCP_CODES else "read_failed"


async def bars_response(reader, universe, cache, symbols_text, tf, can_read, now=None):
    now = now or utcnow()
    try:
        if tf not in TF:
            raise LetfError("bad_tf")
        symbols = parse_symbols(symbols_text, universe, MAX_SYMBOLS_BARS)
        if not can_read:
            raise LetfError("not_connected")
        spec = TF[tf]
        groups = split_calls(symbols, tf)
        start, _ = range_for(tf, now)

        async def fetch():
            got = {}
            for g in groups:
                got.update(await reader.bars(g, spec["interval"], start))
            series = {}
            for s in symbols:
                row = got.get(s) or {"bars": [], "dropped": 0}
                rows, forming = aggregate(row["bars"], spec["k"], spec["fetch_ms"])
                series[s] = {"n": len(rows), "dropped": row["dropped"], "bars": rows}
                if spec["k"] > 1:
                    series[s]["forming"] = forming
            return series

        series, cached = await cache.get(("bars", tuple(symbols), tf), BARS_TTL_S[tf], fetch, calls=len(groups))
    except Exception as e:  # noqa: BLE001 - every failure is reduced to a fixed code
        return fail(_code_of(e))
    return {"ok": True, "tf": tf, "interval": spec["interval"], "bar_ms": spec["fetch_ms"] * spec["k"],
            "as_of_ms": int(now.timestamp() * 1000), "session_open": equity_session_open(now), "cached": cached, "series": series}


_Q_FIELDS = ("bid", "ask", "last", "prev_close", "updated_ms")


async def quotes_response(reader, universe, cache, symbols_text, can_read, now=None):
    now = now or utcnow()
    try:
        symbols = parse_symbols(symbols_text, universe, MAX_SYMBOLS_QUOTES)
        if not can_read:
            raise LetfError("not_connected")

        async def fetch():
            got = await reader.quotes(symbols)
            out = {}
            for s in symbols:
                q = got.get(s)
                row = {}
                if q:
                    for k in ("bid", "ask", "last", "prev_close"):
                        if _finite(q.get(k)):
                            row[k] = q[k]
                    if _finite(q.get("updated_at")):
                        row["updated_ms"] = int(q["updated_at"] * 1000)
                row["unknown"] = [k for k in _Q_FIELDS if k not in row]     # missing is listed, never turned into 0
                out[s] = row
            return out

        quotes, cached = await cache.get(("quotes", tuple(symbols)), QUOTES_TTL_S, fetch)
    except Exception as e:  # noqa: BLE001
        return fail(_code_of(e))
    return {"ok": True, "as_of_ms": int(now.timestamp() * 1000), "cached": cached, "quotes": quotes}
