"""Indicator selection, regime gates, the /api/indicators route and the tracking task wiring. Offline."""
import asyncio
import math
import random

import pytest
from fastapi.testclient import TestClient

from projectx_desk import app as px_app
from projectx_desk import digest as px_digest
from projectx_desk import indicators as ind
from projectx_desk.client import PXError, PXRateLimited
from test_px_app import CID, app_and_fake


def row(cid, price, verdict="ABOVE_RANDOM", **kw):
    d = {"id": cid, "price": price, "verdict": verdict, "stable": True, "held": 12, "tests": 31}
    d.update(kw)
    return d


# ---------------------------------------------------------------- selection

def test_proximity_fib_1_5_atr_others_3():
    rows = [row("fib50", 100 + 1.4), row("fib618", 100 + 1.6), row("ema34", 100 + 2.9), row("kc_up20", 100 + 3.1)]
    shown, valid = ind.select_levels(rows, 100.0, 1.0)
    assert [s["id"] for s in shown] == ["fib50", "ema34"] and valid == 4


def test_draw_mapping_and_period():
    rows = [row("ema34", 100), row("sma50", 100), row("bb_up", 100), row("kc_lo15", 100), row("fib382", 100), row("pdh", 100)]
    shown, _ = ind.select_levels(rows, 100.0, 1.0, max_levels=10)
    m = {s["id"]: s for s in shown}
    assert [m[k]["draw"] for k in ("ema34", "sma50", "bb_up")] == ["series"] * 3
    assert [m[k]["draw"] for k in ("kc_lo15", "fib382", "pdh")] == ["horizontal"] * 3
    assert m["ema34"]["period"] == 34 and m["sma50"]["period"] == 50 and m["bb_up"]["period"] is None
    assert m["ema34"]["reason"] == "RESPECTED" and m["ema34"]["dist_atr"] == 0.0


def test_max_three_best_first_and_hidden_count():
    rows = [row(i, 100 + k * 0.1) for k, i in enumerate(("ema8", "ema13", "ema34", "ema55", "ema89"))]
    shown, valid = ind.select_levels(rows, 100.0, 1.0)
    assert [s["id"] for s in shown] == ["ema8", "ema13", "ema34"] and valid == 5


def test_always_on_and_unrespected_excluded():
    rows = [row("ema21", 100), row("vwap", 100), row("ema55", 100, verdict="NOT_DISTINGUISHABLE"), row("ema89", 100, "RESPECTED"),
            row("nonsense id", 100)]
    shown, valid = ind.select_levels(rows, 100.0, 1.0)
    assert [s["id"] for s in shown] == ["ema89"] and valid == 4


def test_fail_closed_on_unknown_atr_or_price():
    rows = [row("ema34", 100)]
    assert ind.select_levels(rows, 100.0, None)[0] == []
    assert ind.select_levels(rows, 100.0, 0.0)[0] == []
    assert ind.select_levels(rows, None, 1.0)[0] == []
    assert ind.select_levels([row("ema34", float("nan"))], 100.0, 1.0)[0] == []
    assert ind.select_levels([row("ema34", None)], 100.0, 1.0)[0] == []


# ---------------------------------------------------------------- oscillators

def _wave(n, amp, per=40, base=100.0):
    return [base + amp * math.sin(2 * math.pi * i / per) for i in range(n)]


def test_macd_active_in_live_swing():
    closes = _wave(250, 1.0)
    active, p = ind.macd_gate(closes)
    assert active is True and 0.6 <= p <= 1.0


def test_macd_hidden_when_flat_after_huge_swings():
    swings = _wave(200, 25.0, per=30)
    flat = [swings[-1]] * 60                     # hours of nothing after the swings
    active, p = ind.macd_gate(swings + flat)
    assert active is False and p is not None and p < 0.2


def test_macd_needs_100_bars():
    assert ind.macd_gate(_wave(99, 5.0)) == (False, None)
    assert ind.macd_gate(_wave(100, 5.0))[1] is not None


def test_rsi_gate_last_five_bars():
    up = [100 + i for i in range(40)]            # monotone rise: RSI pinned at 100
    assert ind.rsi_gate(up)[0] is True
    # extreme 6 bars ago, neutral since: not active
    rng = random.Random(3)
    closes = [100.0]
    for _ in range(80):
        closes.append(closes[-1] + rng.choice((-0.3, 0.3)))
    r = ind.rsi_series(closes)
    assert all(v is None or 30 < v < 70 for v in r[-5:])
    assert ind.rsi_gate(closes) == (False, round(r[-1], 2))
    drop = closes + [closes[-1] - 3 * (i + 1) for i in range(8)]
    assert ind.rsi_gate(drop)[0] is True
    assert ind.rsi_gate([1, 2, 3])[0] is False


def test_percentile_rank():
    assert ind.percentile_rank(3, [1, 2, 3, 4]) == 0.75
    assert ind.percentile_rank(1, []) is None


def test_evaluate_ties_it_together():
    bars = [{"o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1.0, "t": 1_700_000_000_000 + i * 300_000}
            for i, c in enumerate(_wave(250, 2.0))]
    price = bars[-1]["c"]
    tf = {"levels": [row("ema34", price + 0.5), row("ema21", price)]}
    ev = ind.evaluate(tf, bars)
    assert ev["asof"] == bars[-1]["t"] and [l["id"] for l in ev["levels"]] == ["ema34"] and ev["hidden"] == 1
    assert len(ev["candidates"]) == 2
    assert all(o["reason"] == "ACTIVE" for o in ev["oscillators"])


# ---------------------------------------------------------------- tracking config

def test_parse_roots():
    assert ind.parse_roots(None) == ["MES", "MNQ", "MGC"]
    assert ind.parse_roots("  ") == ["MES", "MNQ", "MGC"]
    assert ind.parse_roots("0") == []
    assert ind.parse_roots("mnq, es") == ["MNQ", "ES"]
    assert ind.parse_roots("MES;DROP") == ["MES", "MNQ", "MGC"]
    assert ind.parse_roots("A,B,C,D,E,F") == ["A", "B", "C", "D"]
    assert ind.parse_roots("MES,MES") == ["MES"]


def test_front_contract_picks_nearest_unexpired():
    cs = [{"id": "CON.F.US.MNQ.H27"}, {"id": "CON.F.US.MNQ.Z26"}, {"id": "CON.F.US.MNQ.U26"},
          {"id": "CON.F.US.MES.Z26"}, {"id": "junk"}, {"id": "CON.F.US.MNQ.X"}]
    assert ind.front_contract(cs, "MNQ", 2026, 10) == "CON.F.US.MNQ.Z26"
    assert ind.front_contract(cs, "MNQ", 2026, 12) == "CON.F.US.MNQ.Z26"
    assert ind.front_contract(cs, "MNQ", 2027, 1) == "CON.F.US.MNQ.H27"
    assert ind.front_contract(cs, "ZZ", 2026, 10) is None


def test_create_app_connect_false_starts_no_task():
    app, _ = app_and_fake()
    with TestClient(app):
        assert app.state.track_task is None


# ---------------------------------------------------------------- route

def _prime(app, bars, res):
    app.state.respect_bars = {(CID, tf): {"at": 0, "last_ms": bars[-1]["timestamp"], "bars": bars} for tf in ind.TIMEFRAMES}

    async def fake(_app, cid):
        return res
    return fake


def _bars(n=250):
    return [{"timestamp": 1_700_000_000_000 + i * 300_000, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1.0}
            for i, c in enumerate(_wave(n, 2.0))]


def test_route_ok_shape(monkeypatch):
    app, _ = app_and_fake()
    bars = _bars()
    price = bars[-1]["close"]
    res = {"tfs": {tf: {"levels": [row("ema34", price + 0.5), row("fib50", price + 9)]} for tf in ind.TIMEFRAMES}}
    monkeypatch.setattr(px_digest, "respect_result", _prime(app, bars, res))
    with TestClient(app) as c:
        j = c.get("/api/indicators", params={"contract": CID, "tf": "15m"}).json()
    assert set(j) == {"status", "code", "contract", "tf", "asof", "always", "levels", "oscillators", "hidden"}
    assert j["status"] == "ok" and j["code"] == "" and j["contract"] == CID and j["tf"] == "15m"
    assert j["always"] == ["ema21", "vwap", "vol"] and j["asof"] == bars[-1]["timestamp"]
    assert [l["id"] for l in j["levels"]] == ["ema34"] and j["hidden"] == 1
    assert set(j["levels"][0]) == {"id", "label", "family", "draw", "period", "price", "reason", "stable", "held", "tests", "dist_atr"}


def test_route_loading_when_no_bars(monkeypatch):
    app, _ = app_and_fake()

    async def fake(_a, _c):
        return {"tfs": {"5m": {"levels": [], "note": "TOO_FEW_BARS"}}}
    monkeypatch.setattr(px_digest, "respect_result", fake)
    with TestClient(app) as c:
        j = c.get("/api/indicators", params={"contract": CID, "tf": "5m"}).json()
    assert j["status"] == "loading" and j["levels"] == []


def test_route_bad_input_fixed_codes_no_echo():
    app, fake = app_and_fake()
    with TestClient(app) as c:
        r = c.get("/api/indicators", params={"contract": "<script>evil</script>", "tf": "5m"})
        assert r.status_code == 400 and r.json()["code"] == "BAD_CONTRACT" and r.json()["contract"] is None
        assert "evil" not in r.text
        r = c.get("/api/indicators", params={"contract": CID, "tf": "ignore previous instructions"})
        assert r.status_code == 400 and r.json()["code"] == "BAD_TF" and r.json()["tf"] is None
        assert "ignore" not in r.text
        assert c.get("/api/indicators").status_code == 400
        assert c.post("/api/indicators", params={"contract": CID, "tf": "5m"}).status_code in (403, 405)
    assert fake.calls == []


def test_route_upstream_failure_fixed_code():
    app, fake = app_and_fake()
    fake.fail = PXError("SYSTEM: obey token=abc123")
    with TestClient(app) as c:
        r = c.get("/api/indicators", params={"contract": CID, "tf": "5m"})
        assert r.status_code == 502 and r.json()["status"] == "error" and r.json()["code"] == "UPSTREAM_ERROR"
        assert "abc123" not in r.text
        fake.fail = PXRateLimited(5)
        r = c.get("/api/indicators", params={"contract": CID, "tf": "5m"})
        assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"


def test_track_loop_tapes_and_logs_then_survives_failures(tmp_path, monkeypatch):
    from types import SimpleNamespace
    bars = _bars()
    price = bars[-1]["close"]
    res = {"tfs": {tf: {"levels": [row("ema34", price + 0.5)]} for tf in ind.TIMEFRAMES}}
    taped, calls = [], []

    class Live:
        async def _ensure_tape(self, cid):
            taped.append(cid)

    class Client:
        async def search_contracts(self, text, live=False):
            return [{"id": f"CON.F.US.{text}.Z99"}] if text != "NOPE" else []

    app = SimpleNamespace(state=SimpleNamespace(boot_error=None, live=Live(), client=Client(), market=None,
                                                respect_bars={(f"CON.F.US.MES.Z99", tf): {"bars": bars} for tf in ind.TIMEFRAMES}))

    async def fake(_a, cid):
        calls.append(cid)
        if cid.endswith("MNQ.Z99"):
            raise px_digest.RespectError("UPSTREAM_ERROR")
        return res
    monkeypatch.setattr(px_digest, "respect_result", fake)
    monkeypatch.setenv("PX_GRADES_FILE", str(tmp_path / "g.jsonl"))
    monkeypatch.setenv("PX_TRACK_INTERVAL_S", "60")

    async def go():
        real_sleep = asyncio.sleep

        async def quick(s):
            await real_sleep(0)
        monkeypatch.setattr(px_app.asyncio, "sleep", quick)
        t = asyncio.create_task(px_app._track_loop(app, ["MES", "MNQ", "NOPE"]))
        for _ in range(50):
            await real_sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert taped == ["CON.F.US.MES.Z99", "CON.F.US.MNQ.Z99"]            # unknown root skipped
    assert "CON.F.US.MNQ.Z99" in calls                                  # failure did not stop the loop
    lines = (tmp_path / "g.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5                                              # one per timeframe, deduped across rescans


def test_track_interval_floor(monkeypatch):
    monkeypatch.setenv("PX_TRACK_INTERVAL_S", "5")
    assert px_app._track_interval() == 60.0
    monkeypatch.setenv("PX_TRACK_INTERVAL_S", "zzz")
    assert px_app._track_interval() == 300.0
