"""Dealer-gamma (GEX) levels for the desk chart and the AI digest. Display only, advisory.

Source: TraderDaddy public API, `GET {PX_GEX_URL}/gex/{SYMBOL}` with the `X-API-Key` header (same call the
trading-agent client makes). The payload is reduced to an allowlist of validated numbers and enums, converted
into futures price space by `gex_futures.convert_levels`, and returned as a small dict. NOTHING free-form
crosses this module: no `interpretation` text, no URLs, no upstream error text.

GEX marks positioning, not targets; the flip is a regime boundary, not a forecast. These levels feed the chart
and the digest ONLY. They must never reach sizing, stops, tickets or the guard (px_guard does not import this).

Config (env, all optional):
  PX_GEX_KEY        TraderDaddy API key (falls back to TD_API_KEY). Unset = status NO_KEY. Never logged or returned.
  PX_GEX_URL        API base, default https://api.traderdaddy.pro/api/v1. https only, or http to loopback.
  PX_GEX_MAX_AGE_S  refuse levels older than this, 60..3600, default 900. convert_levels' own default is 300 s;
                    we pass this value as its `max_age_s` so the two never disagree. GEX moves slowly and the
                    upstream refreshes about once a minute, so 15 minutes is stale-enough-to-distrust without
                    blanking the chart on one missed refresh.
"""
import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from projectx_desk import gex_futures
from projectx_desk import sizing
DEFAULT_URL = "https://api.traderdaddy.pro/api/v1"
DEFAULT_MAX_AGE_S = 900.0
TTL_S = 300.0              # successful fetch cache, per source symbol
ERR_TTL_S = 60.0           # a failed fetch is remembered briefly so an outage is not hammered
MAX_WALLS = 6
MAX_DIST_PCT = 0.03        # drop levels further than this from the futures last
MAX_BODY = 2_000_000
_SYMS = frozenset(s["symbol"] for row in gex_futures.MAPPING.values() for s in row["sources"])
_WALL_TYPES = {"resistance": "RESISTANCE", "support": "SUPPORT"}      # "pin" is carried separately


class GexError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ config

def gex_config(environ=None):
    """(base_url, key, max_age_s), or None when no key is set. Raises GexError('CONFIG') on an unsafe URL."""
    env = os.environ if environ is None else environ
    base = (env.get("PX_GEX_URL") or DEFAULT_URL).strip().rstrip("/")
    u = urlparse(base)
    loop = u.hostname in ("127.0.0.1", "localhost", "::1")
    if not u.hostname or u.scheme not in ("https", "http") or (u.scheme == "http" and not loop):
        raise GexError("CONFIG")                         # never send the key in cleartext off-box
    key = (env.get("PX_GEX_KEY") or env.get("TD_API_KEY") or "").strip()
    if not key:
        return None
    try:
        age = float(env.get("PX_GEX_MAX_AGE_S") or DEFAULT_MAX_AGE_S)
    except ValueError:
        age = DEFAULT_MAX_AGE_S
    if not math.isfinite(age):
        age = DEFAULT_MAX_AGE_S
    return base, key, max(60.0, min(age, 3600.0))


# ------------------------------------------------------------------ fetch

async def fetch_gex(base, key, symbol):
    """Raw JSON dict for one source symbol. Raises GexError with a fixed code only."""
    if symbol not in _SYMS:
        raise GexError("SYMBOL")
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as c:
            async with c.stream("GET", f"{base}/gex/{symbol}", headers={"X-API-Key": key, "Authorization": f"Bearer {key}"}) as r:
                if r.status_code in (401, 403):
                    raise GexError("AUTH")
                if r.status_code != 200:
                    raise GexError("UPSTREAM_ERROR")
                buf = bytearray()
                async for chunk in r.aiter_bytes():          # the cap is enforced while reading, not after the body is in memory
                    buf += chunk
                    if len(buf) > MAX_BODY:
                        raise GexError("UPSTREAM_ERROR")
    except httpx.HTTPError:
        raise GexError("UPSTREAM_ERROR") from None
    try:
        data = json.loads(bytes(buf))
    except (ValueError, RecursionError):
        raise GexError("UPSTREAM_ERROR") from None
    if not isinstance(data, dict):
        raise GexError("UPSTREAM_ERROR")
    return data


# ------------------------------------------------------------------ sanitize (pure)

def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if lo is not None and v <= lo:
        return None
    if hi is not None and abs(v) > hi:
        return None
    return float(v)


def _parse_epoch(s):
    if not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def sanitize_payload(raw, symbol):
    """Raw API dict -> {symbol, spot, flip, pin, walls[{strike, type, gex}], updated_epoch} or raises GexError('BAD_PAYLOAD').
    Only validated numbers and enums survive. flip/pin/walls are individually optional; spot and the timestamp are not."""
    if not isinstance(raw, dict):
        raise GexError("BAD_PAYLOAD")
    spot = _num(raw.get("spotPrice"), 0, 1e7)
    upd = _parse_epoch(raw.get("lastUpdated"))
    if spot is None or upd is None:
        raise GexError("BAD_PAYLOAD")

    def strike(v):                                       # a level within 50% of spot or it is garbage
        x = _num(v, 0, 1e7)
        return x if x is not None and abs(x / spot - 1.0) <= 0.5 else None

    walls, seen = [], set()
    kl = raw.get("keyLevels")
    for it in (kl if isinstance(kl, list) else [])[:200]:
        if not isinstance(it, dict):
            continue
        typ = _WALL_TYPES.get(it.get("type")) if isinstance(it.get("type"), str) else None
        k, g = strike(it.get("strike")), _num(it.get("netGEX"), None, 1e13)
        if typ is None or k is None or g is None or k in seen:
            continue
        seen.add(k)
        walls.append({"strike": k, "type": typ, "gex": g})
    walls.sort(key=lambda w: -abs(w["gex"]))
    return {"symbol": symbol, "spot": spot, "flip": strike(raw.get("gammaFlipLevel")),
            "pin": strike(raw.get("maxGammaStrike")), "walls": walls[:MAX_WALLS], "updated_epoch": upd}


def quote_last(q):
    """Mid when both sides are readable, else last; None when nothing usable."""
    if not isinstance(q, dict):
        return None
    b, a, l = (_num(q.get(k), 0, 1e7) for k in ("bestBid", "bestAsk", "lastPrice"))
    if b is not None and a is not None and a >= b:
        return (a + b) / 2.0
    return l


def build_levels(clean, age_s):
    """sanitize_payload output -> the levels dict gex_futures.convert_levels expects."""
    lv = {"symbol": clean["symbol"], "spot": clean["spot"], "walls": [{"strike": w["strike"], "type": w["type"]} for w in clean["walls"]]}
    if clean["flip"] is not None:
        lv["flip"] = clean["flip"]
    if clean["pin"] is not None:
        lv["pin"] = clean["pin"]
    return lv


def shape_levels(res, last, tick):
    """Converted FuturesLevels -> list of {name, price, side, pts, ticks}, nearest-first-agnostic, high to low.
    Names: flip, pin, wall_above, wall_below (walls are named by where they sit versus last, not by source type)."""
    lv, out, dropped = res.levels, [], 0
    cand = []
    for k in ("flip", "pin"):
        if lv.get(k) is not None:
            cand.append((k, lv[k], None))
    for w in lv.get("walls") or []:
        cand.append(("wall_above" if w["strike"] > last else "wall_below", w["strike"], w.get("type")))
    for name, price, typ in cand:
        if abs(price - last) / last > MAX_DIST_PCT:
            dropped += 1
            continue
        d = price - last
        e = {"name": name, "price": price, "side": "above" if d > 0 else "below" if d < 0 else "at",
             "pts": round(d, 2), "ticks": int(round(d / tick))}
        if typ:
            e["wall_type"] = typ
        out.append(e)
    out.sort(key=lambda e: -e["price"])
    return out, dropped


# ------------------------------------------------------------------ cached result

def _base(status, **kw):
    return {"v": 1, "status": status, "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "levels": [], "disclaimer": gex_futures.DISCLAIMER} | kw


async def _source(st, base, key, symbol, fetch):
    """(clean | None, code | None) for one source symbol, cached; caller holds st.gex_lock."""
    hit = st.gex_cache.get(symbol)
    if hit and time.monotonic() - hit[0] < (TTL_S if hit[1] else ERR_TTL_S):
        return hit[1], hit[2]
    try:
        clean, code = sanitize_payload(await fetch(base, key, symbol), symbol), None
    except GexError as e:
        clean, code = None, e.code
    except Exception:  # noqa: BLE001
        clean, code = None, "UPSTREAM_ERROR"
    st.gex_cache[symbol] = (time.monotonic(), clean, code)
    return clean, code


async def gex_result(app, contract_id, last=None, environ=None, fetch=fetch_gex, now=None):
    """Always returns a dict: status OK | STALE | UNAVAILABLE | NO_KEY | ERROR (+ fixed `code`). Never raises.
    `last` is the futures last/mid; when None it is read from the live quote of `contract_id`."""
    try:
        return await _gex_result(app, contract_id, last, environ, fetch, now)
    except Exception:  # noqa: BLE001
        return _base("ERROR", code="INTERNAL")


async def _gex_result(app, contract_id, last, environ, fetch, now):
    st = app.state
    if not contract_id:
        return _base("UNAVAILABLE", code="NO_CONTRACT")
    try:
        cfg = gex_config(environ)
    except GexError as e:
        return _base("ERROR", code=e.code)
    if cfg is None:
        return _base("NO_KEY")
    base, key, max_age = cfg
    try:
        spec = sizing.spec_for(contract_id)
    except sizing.SizingError:
        return _base("UNAVAILABLE", code="CONTRACT")
    root = gex_futures.resolve_root(spec.root)
    if root is None or gex_futures.MAPPING[root].get("supported", True) is False:
        return _base("UNAVAILABLE", code="UNSUPPORTED")
    if last is None:
        q = getattr(getattr(st, "live", None), "quotes", {}).get(contract_id)
        last = quote_last(q)
    last = _num(last, 0, 1e7)
    if last is None:
        return _base("UNAVAILABLE", code="NO_QUOTE")

    if not hasattr(st, "gex_lock"):
        st.gex_lock = asyncio.Lock()
        st.gex_cache = {}
    now_ts = time.time() if now is None else now
    codes, stale = [], False
    for src in gex_futures.MAPPING[root]["sources"]:
        sym = src["symbol"]
        async with st.gex_lock:                           # single flight: one fetch per symbol per TTL
            clean, code = await _source(st, base, key, sym, fetch)
        if clean is None:
            codes.append(code)
            continue
        age = now_ts - clean["updated_epoch"]
        if age < -60:                                    # a timestamp from the future is not proof of freshness
            codes.append("BAD_PAYLOAD")
            continue
        age = max(0.0, age)
        if age > max_age:
            stale = True
            continue
        res = gex_futures.convert_levels(build_levels(clean, age), spec.root, last, clean["spot"], spec.tick_size, age,
                                         max_age_s=max_age, source_symbol=sym)
        if not res.ok:
            codes.append("CONVERT")
            continue
        levels, dropped = shape_levels(res, last, spec.tick_size)
        panel = gex_futures.describe_for_panel(res, last)
        return _base("OK", source=sym, method=res.method, basis=res.basis, approximate=res.approximate,
                     flip_split=False, age_s=int(age), max_age_s=int(max_age), dropped_far=dropped,
                     levels=levels, conversion=panel.get("conversion"))
    if stale:
        return _base("STALE", code="STALE", max_age_s=int(max_age))
    if codes and all(c == "AUTH" for c in codes):
        return _base("ERROR", code="AUTH")
    return _base("ERROR" if codes and codes[0] != "CONVERT" else "UNAVAILABLE", code=codes[0] if codes else "NO_SOURCE")


def gex_lines(g):
    """Digest lines (the digest builds its own GEX lines from the same dict; this is for standalone use)."""
    g = g or {"status": "UNAVAILABLE"}
    L = [f"GEX: status={g.get('status')}" + "".join(f" {k}={g[k]}" for k in ("source", "method", "basis", "age_s", "code") if k in g)]
    for n, lv in enumerate(g.get("levels") or [], 1):
        L.append(f"GEX_{n}: {lv['name']} {lv['price']} {lv['pts']}pt {lv['ticks']}t {lv['side']}")
    L.append("GEX_NOTE: positioning not targets; flip is regime boundary")
    return L
