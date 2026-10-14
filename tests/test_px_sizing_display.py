"""The size box must always say WHY: ATR, ADX, scaler cuts, stop math, risk-vs-cap, allowed or not."""
import math
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from test_px_app_actions import make_app

RECS = {
    "CON.F.US.MNQ.Z26": {"id": "CON.F.US.MNQ.Z26", "name": "MNQZ6", "activeContract": True, "tickSize": 0.25, "tickValue": 0.5},
    "CON.F.US.NQ.Z26": {"id": "CON.F.US.NQ.Z26", "name": "NQZ6", "activeContract": True, "tickSize": 0.25, "tickValue": 5.0},
    "CON.F.US.MES.Z26": {"id": "CON.F.US.MES.Z26", "name": "MESZ6", "activeContract": True, "tickSize": 0.25, "tickValue": 1.25},
}


def bars_newest_first(n=300, atr=1.0, base=17500.0, trend=0.0, amp=2.0):
    """Synthetic 5m bars: sine + optional drift; each bar's range is about `atr` points."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    out = []
    prev = base
    for i in range(n):                                   # oldest first, then reversed
        c = base + trend * i + amp * math.sin(i / 5.0)
        h, l = max(prev, c) + atr / 2, min(prev, c) - atr / 2
        out.append({"t": (now - timedelta(minutes=5 * (n - i))).isoformat(), "o": prev, "h": h, "l": l, "c": c, "v": 100 + i % 7})
        prev = c
    return list(reversed(out))


def rig(tmp_path, atr, trend=0.0, amp=2.0, scalers=True, **limits):
    app, g, live, gw = make_app(tmp_path, size_scalers=scalers, **limits)
    return app, g, live


def prime(app, g, live, atr, trend=0.0, amp=2.0):
    async def lookup(cid):
        return RECS[cid]

    async def bars(body):
        return bars_newest_first(atr=atr, trend=trend, amp=amp)

    g._lookup = lookup
    g._contracts.clear()
    app.state.client.bars = bars
    app.state.__dict__.pop("ind_cache", None)
    for cid in RECS:
        live.quotes[cid] = {"bestAsk": 17500.5, "bestBid": 17500.25, "lastPrice": 17500.25, "_rx": g.clock()}


def digest(c, cid, **q):
    return c.get("/ai/digest", params={"contract": cid, "risk_pct": 0.05, **q}).text


def test_nq_and_mnq_same_volatility_same_count_but_the_why_shows_the_real_difference(tmp_path):
    # ATR 0.8 pt on both (the mock's calm world): both fit, the cap is what makes both "1".
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MNQ", "NQ", "MES", "MGC", "MCL"), max_qty_order=1)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        m = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()["size"]
        n = c.get("/ai/state", params={"contract": "CON.F.US.NQ.Z26", "risk_pct": 0.05}).json()["size"]
        assert m["contracts"] == 1 and n["contracts"] == 1                       # the headline really is the same...
        assert m["rpc"] == 10 * 0.5 * (m["stop_ticks"] / 10) and n["rpc"] == n["stop_ticks"] * 5.0
        assert n["rpc"] / m["rpc"] > 8                                           # ...but a contract of NQ risks ~10x more
        assert m["contracts_by_risk"] > n["contracts_by_risk"]                   # risk alone would allow far more MNQ
        assert m["capped"] and any("cap PX_MAX_QTY_ORDER=1" in w for w in m["why"])
        assert any("Risk alone allows" in w for w in m["why"])
        text = digest(c, "CON.F.US.MNQ.Z26")
        assert "SIZE_WHY: Risk: 5% of" in text and "risk_allows=" in text and "cap=1" in text


def test_real_nq_volatility_is_refused_and_says_why(tmp_path):
    app, g, live = rig(tmp_path, atr=25.0, allowed_roots=("MNQ", "NQ"), max_qty_order=1)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=25.0)
        nq = c.get("/ai/state", params={"contract": "CON.F.US.NQ.Z26", "risk_pct": 0.05}).json()["size"]
        assert nq["refused"] and nq["reason"] == "GRANULARITY" and nq["contracts"] == 0
        assert any(w.startswith("REFUSED: one NQ risks") and "Never rounded up" in w for w in nq["why"])
        assert nq["stop_ticks"] > 100                                            # 1.5 x 25pt = 37.5pt = 150 ticks


def test_disallowed_instrument_is_flagged_not_silently_sized(tmp_path):
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MES", "MNQ"))
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        z = c.get("/ai/state", params={"contract": "CON.F.US.NQ.Z26", "risk_pct": 0.05}).json()["size"]
        assert z["allowed"] is False and any("not in PX_ALLOWED_ROOTS" in w for w in z["why"])
        assert "SIZE_ALLOWED: NO (NQ" in digest(c, "CON.F.US.NQ.Z26")
        ok = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()["size"]
        assert ok["allowed"] is True


def inject_ind(monkeypatch, adx, pctile, atr=0.8):
    """Smooth synthetic bars always give a high ADX, so inject known values to pin the scaler thresholds."""
    import px_digest

    async def fake(app, cid):
        return {"atr": atr, "adx": adx, "atr_pctile": pctile, "tf": "5m", "vwap": 17500.0, "hi": 17510.0, "lo": 17490.0,
                "volume": 1000.0, "bars": 300, "ema": {"state": "MIXED", "8": 1.0, "21": 1.0, "34": 1.0}, "levels": []}

    monkeypatch.setattr(px_digest, "indicators", fake)


def size_for(tmp_path, monkeypatch, adx, pctile, **limits):
    inject_ind(monkeypatch, adx, pctile)
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MNQ",), **limits)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        return c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()["size"]


def test_chop_cuts_risk_and_the_why_says_adx_and_the_multiplier(tmp_path, monkeypatch):
    z = size_for(tmp_path, monkeypatch, adx=15.0, pctile=50.0)
    assert z["scale"]["reduced"] and z["scale"]["multiplier"] == 0.67
    assert any("ADX 15.0 < 20" in w and "chop" in w and "x0.67" in w for w in z["why"])
    assert any(w.startswith("After cuts:") for w in z["why"])
    assert z["budget_usd"] == round(z["budget_chosen_usd"] * 0.67, 2) or abs(z["budget_usd"] - z["budget_chosen_usd"] * 0.67) < 0.02


def test_trending_and_normal_volatility_means_no_cut(tmp_path, monkeypatch):
    z = size_for(tmp_path, monkeypatch, adx=32.0, pctile=40.0)
    assert not z["scale"]["reduced"] and z["scale"]["multiplier"] == 1.0
    assert z["budget_usd"] == z["budget_chosen_usd"]
    assert not any(w.startswith("After cuts:") for w in z["why"])


def test_high_atr_percentile_cuts_and_stacks_with_chop(tmp_path, monkeypatch):
    hi = size_for(tmp_path, monkeypatch, adx=32.0, pctile=92.0)
    assert hi["scale"]["multiplier"] == 0.67 and any("92nd percentile" in w or "92th percentile" in w for w in hi["why"])
    both = size_for(tmp_path, monkeypatch, adx=15.0, pctile=92.0)
    assert both["scale"]["multiplier"] < 0.5 and len(both["scale"]["reasons"]) == 2


def test_unknown_indicators_shrink_risk_and_say_unknown(tmp_path, monkeypatch):
    z = size_for(tmp_path, monkeypatch, adx=None, pctile=None)
    assert z["scale"]["multiplier"] == 0.5625 and any("unknown" in w for w in z["why"])


def test_scalers_off_means_no_cut_and_it_says_so(tmp_path):
    app, g, live = rig(tmp_path, atr=0.8, scalers=False)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        z = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()["size"]
        assert z["scale"]["multiplier"] == 1.0 and any("scalers off" in w for w in z["why"])
        assert z["budget_usd"] == z["budget_chosen_usd"]


def test_the_staged_ticket_uses_the_same_scaled_risk_as_the_size_box(tmp_path):
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MNQ",))
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        z = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()["size"]
        from test_px_app_actions import post
        tok = c.get("/api/csrf").json()["token"]
        r = post(c, "/api/preview", tok, {"contract": "CON.F.US.MNQ.Z26", "side": "long", "stop_ticks": z["stop_ticks"],
                                          "risk_pct": 0.05}).json()
        assert r["ok"], r["reasons"]
        assert r["scale"]["multiplier"] == z["scale"]["multiplier"]
        assert r["ticket"]["readback"]["risk_usd"] <= z["budget_usd"] + 0.01      # never above what the box promised


def test_stream_mode_why_has_no_dollars(tmp_path):
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MNQ",))
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        z = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05, "stream": 1}).json()["size"]
        assert z["why"] and not any("$" in w for w in z["why"]) and "why_stream" not in z
        assert not any(k in z for k in ("budget_usd", "rpc", "risk_usd"))
        normal = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()["size"]
        assert "why_stream" not in normal and any("$" in w for w in normal["why"])


def test_ema_stack_and_level_reactions_are_in_state_and_digest(tmp_path):
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MNQ",))
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=1.2, trend=0.05, amp=3.0)
        s = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26"}).json()
        assert s["ind"]["ema"]["state"] in ("BULL", "BEAR", "MIXED") and {"8", "21", "34"} <= set(s["ind"]["ema"])
        names = [lv["name"] for lv in s["levels"]]
        assert names == ["EMA8", "EMA21", "EMA34", "VWAP"] or set(names) >= {"EMA8", "EMA21", "EMA34"}
        for lv in s["levels"]:
            assert lv["n"] == lv["bounced"] + lv["failed"] + lv["neutral"]
            assert lv["small_sample"] == (lv["n"] < 8)
        text = digest(c, "CON.F.US.MNQ.Z26")
        assert "EMA: stack=" in text and "LEVEL: EMA21 " in text and "hold_rate=" in text
        assert "atr_pctile=" in text
