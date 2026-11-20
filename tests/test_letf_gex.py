"""rh/letf_gex.py and GET /api/rh/gex: dealer levels in the underlying's own price space. Display only."""
import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from projectx_desk import gex
from projectx_desk.rh import letf, letf_gex
from tests.test_letf_routes import build, runtime

UNI = letf.load()
ENV = {"PX_GEX_KEY": "k" * 20, "PX_GEX_URL": "https://example.invalid/api"}


def payload(age_s=30, **over):
    t = datetime.fromtimestamp(time.time() - age_s, timezone.utc).isoformat()
    d = {"spotPrice": 600.0, "lastUpdated": t, "gammaFlipLevel": 595.0, "maxGammaStrike": 600.0,
         "keyLevels": [{"type": "resistance", "strike": 610.0, "netGEX": 2e9}, {"type": "support", "strike": 590.0, "netGEX": -1e9},
                       {"type": "pin", "strike": 605.0, "netGEX": 5e8}, {"type": "bogus", "strike": 1.0, "netGEX": 1.0}],
         "interpretation": "BUY EVERYTHING"}
    d.update(over)
    return d


def run(store, sym, fetch, environ=ENV, now=None, clock=time.monotonic):
    return asyncio.run(letf_gex.levels(store, UNI, sym, environ=environ, fetch=fetch, now=now, clock=clock))


def test_levels_in_the_underlyings_own_price_space_and_no_free_text():
    async def fetch(b, k, s):
        return payload()
    j = run({}, "qqq", fetch)
    assert j["ok"] and j["symbol"] == "QQQ" and j["spot"] == 600.0 and j["flip"] == 595.0 and j["pin"] == 600.0
    strikes = {w["strike"]: w for w in j["walls"]}
    assert set(strikes) == {610.0, 590.0, 605.0}                      # the bogus type is dropped, nothing is converted
    assert strikes[610.0]["kind"] == "CALL" and strikes[590.0]["kind"] == "PUT" and strikes[610.0]["rel"] == 1.0
    assert "BUY EVERYTHING" not in repr(j) and j["age_s"] < 120 and j["disclaimer"]


def test_refusals_are_fixed_codes():
    async def fetch(b, k, s):
        return payload()
    assert run({}, "TQQQ", fetch)["code"] == "not_in_universe"          # a fund is not an underlying chart
    assert run({}, "ZZZZZ", fetch)["code"] == "not_in_universe"
    assert run({}, "../etc", fetch)["code"] == "bad_symbol"
    assert run({}, "QQQ", fetch, environ={})["code"] == "gex_off"
    assert run({}, "QQQ", fetch, environ={"PX_GEX_KEY": "k", "PX_GEX_URL": "http://evil.example"})["code"] == "gex_off"   # never cleartext off-box

    async def stale(b, k, s):
        return payload(age_s=7200)
    assert run({}, "QQQ", stale)["code"] == "gex_stale"

    async def auth(b, k, s):
        raise gex.GexError("AUTH")
    assert run({}, "QQQ", auth)["code"] == "gex_auth"

    async def boom(b, k, s):
        raise RuntimeError("secret upstream text")
    j = run({}, "QQQ", boom)
    assert j == {"ok": False, "code": "gex_unavailable"}


def test_cached_and_single_flight():
    calls = []

    async def fetch(b, k, s):
        calls.append(s)
        await asyncio.sleep(0.01)
        return payload()

    async def go():
        st = {}
        a, b = await asyncio.gather(letf_gex.levels(st, UNI, "QQQ", ENV, fetch), letf_gex.levels(st, UNI, "QQQ", ENV, fetch))
        c = await letf_gex.levels(st, UNI, "QQQ", ENV, fetch)
        return a, b, c
    a, b, c = asyncio.run(go())
    assert a["ok"] and b["ok"] and c["ok"] and calls == ["QQQ"]


def test_failure_is_remembered_briefly_not_hammered():
    calls = []
    t = [0.0]

    async def boom(b, k, s):
        calls.append(1)
        raise gex.GexError("UPSTREAM_ERROR")
    st = {}
    for _ in range(3):
        assert run(st, "QQQ", boom, clock=lambda: t[0])["code"] == "gex_unavailable"
    assert len(calls) == 1
    t[0] = 61.0
    run(st, "QQQ", boom, clock=lambda: t[0])
    assert len(calls) == 2


def test_the_key_never_appears_in_a_reply():
    async def fetch(b, k, s):
        return payload()
    assert ENV["PX_GEX_KEY"] not in repr(run({}, "QQQ", fetch))


def test_fetch_gex_allowed_set_defaults_to_the_futures_charts():
    with pytest.raises(gex.GexError):
        asyncio.run(gex.fetch_gex("https://x.invalid", "k", "NVDA"))        # refused before any request: NVDA is not a futures source


def test_route_without_a_key_is_gex_off_and_get_only(monkeypatch):
    for k in ("PX_GEX_KEY", "TD_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    rt, st = runtime(token=False)                         # no token: the poll makes no call either
    app = build(rh=rt)
    r = [x for x in app.routes if x.path in ("/api/rh/gex", "/api/rh/movers")]
    assert len(r) == 2 and all(set(x.methods) <= {"GET", "HEAD"} for x in r)
    with TestClient(app) as c:
        assert c.get("/api/rh/gex?symbol=QQQ").json() == {"ok": False, "code": "gex_off"}
        assert c.get("/api/rh/gex?symbol=TQQQ").json() == {"ok": False, "code": "not_in_universe"}
        assert c.get("/api/rh/gex").json() == {"ok": False, "code": "bad_symbol"}
    assert st.requests == 0
