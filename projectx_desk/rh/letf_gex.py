"""Dealer-gamma levels for the leveraged-ETF desk's underlying chart. DISPLAY ONLY: no guard, sizing, ticket or order path reads this.

It reuses the futures desk's GEX plumbing (`gex.gex_config`, `gex.fetch_gex`, `gex.sanitize_payload`: the key stays in the
header, the body is capped while it is read, and only validated numbers and enums survive) but asks in the underlying's OWN price
space: QQQ's levels are QQQ prices, so nothing is converted. The symbol must be a chart symbol of the vendored universe, so the
route cannot be pointed at an arbitrary ticker. Every refusal is `{"ok": False, "code": <fixed word>}`, never upstream text.

GEX marks positioning, not targets; the flip is a regime boundary, not a forecast.
"""
from __future__ import annotations

import asyncio
import time

from projectx_desk import gex, gex_futures

from .views import SYMBOL

CODES = ("bad_symbol", "not_in_universe", "gex_off", "gex_auth", "gex_stale", "gex_unavailable")
OK_TTL_S = 300.0            # the shared API key is limited to 30 requests a minute: one fetch per symbol per five minutes
ERR_TTL_S = 60.0


def _fail(code):
    return {"ok": False, "code": code}


def shape(clean, age_s, max_age_s):
    """sanitize_payload output -> the reply. `rel` is a wall's size against the biggest wall in the payload; `kind` is the sign of
    its net gamma (PUT negative, CALL positive). Nothing free-form."""
    top = max((abs(w["gex"]) for w in clean["walls"]), default=0.0)
    walls = []
    for w in clean["walls"]:
        e = {"strike": w["strike"], "type": w["type"]}
        if w["gex"] != 0 and top > 0:
            e["kind"] = "PUT" if w["gex"] < 0 else "CALL"
            e["rel"] = round(abs(w["gex"]) / top, 2)
        walls.append(e)
    return {"ok": True, "symbol": clean["symbol"], "spot": clean["spot"], "flip": clean["flip"], "pin": clean["pin"], "walls": walls,
            "age_s": int(age_s), "max_age_s": int(max_age_s), "disclaimer": gex_futures.DISCLAIMER}


async def levels(store, universe, symbol, environ=None, fetch=None, now=None, clock=time.monotonic):
    """`store` is a plain dict the app keeps (cache and locks). `fetch(base, key, symbol)` is injectable for tests."""
    sym = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not SYMBOL.fullmatch(sym):
        return _fail("bad_symbol")
    if sym not in universe.charts:
        return _fail("not_in_universe")
    try:
        cfg = gex.gex_config(environ)
    except gex.GexError:
        return _fail("gex_off")
    if cfg is None:
        return _fail("gex_off")
    base, key, max_age = cfg
    cache, locks = store.setdefault("cache", {}), store.setdefault("locks", {})
    lock = locks.setdefault(sym, asyncio.Lock())
    async with lock:                                              # single flight per symbol
        hit = cache.get(sym)
        if hit is not None and clock() - hit[0] < (OK_TTL_S if hit[1] is not None else ERR_TTL_S):
            clean, code = hit[1], hit[2]
        else:
            fetcher = fetch or (lambda b, k, s: gex.fetch_gex(b, k, s, allowed=frozenset({sym})))
            try:
                clean, code = gex.sanitize_payload(await fetcher(base, key, sym), sym), None
            except gex.GexError as e:
                clean, code = None, e.code
            except Exception:  # noqa: BLE001 - reduced to a fixed word below
                clean, code = None, "UPSTREAM_ERROR"
            cache[sym] = (clock(), clean, code)
    if clean is None:
        return _fail("gex_auth" if code == "AUTH" else "gex_unavailable")
    age = max(0.0, (now if now is not None else time.time()) - clean["updated_epoch"])
    if age > max_age:
        return _fail("gex_stale")
    return shape(clean, age, max_age)
