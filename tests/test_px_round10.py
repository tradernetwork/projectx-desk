"""Round 10 (2026-10-17): what the statistics review changed. docs/DECISIONS.md Round 10.

Open P&L counts against the loss room (fail closed when a position cannot be valued); cost as a share of 1R in the size box
and digest; the respect verdict needs 30 tests, a p-value and an FDR pass; the ADX/ATR scalers default off; the one-click STAGE
on analyst callouts sits behind PX_FEED_STAGE and is audited when used."""
import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from projectx_desk import digest as D
from projectx_desk import feed as px_feed
from projectx_desk import respect as R
from projectx_desk.limits import Limits
from test_px_app_actions import make_app, post
from test_px_digest import state
from test_px_guard_preview import CID, NOW, failed, make, prev

H27 = "CON.F.US.MES.H27"       # same root as CID, another expiry: a second contract the desk may hold


def hold(live, clock, cid=H27, kind=1, size=2, avg=5000.0, last=4990.0, age=0.0, quote=True):
    live.positions[1] = {"id": 1, "accountId": 7, "contractId": cid, "type": kind, "size": size, "averagePrice": avg}
    if quote:
        live.quotes[cid] = {"lastPrice": last, "bestBid": last - 0.25, "bestAsk": last, "_rx": clock() - age}


# ---------------------------------------------------------------------------------- open P&L in the room

async def test_open_loss_is_subtracted_from_both_rooms_and_from_the_size(tmp_path):
    g, live, clock = make(tmp_path, max_qty_symbol=5, max_qty_total=5)
    flat, _ = g.budget(NOW)
    assert flat == 1000.0
    hold(live, clock)                                   # long 2 MES from 5000, last 4990: 40t x $1.25 x 2 = -$100
    assert g.open_pnl() is None                         # the contract record is not cached yet: UNKNOWN, never 0
    await g.mark_positions()
    assert g.open_pnl() == -100.0 and g.draw() == 100.0
    remaining, daily = g.budget(NOW)
    assert (remaining, daily) == (900.0, 900.0)
    p = await prev(g, risk_dollars=1000.0)              # asks for everything: gets the room net of the open loss
    assert p.ok and p.sizing["total_risk_dollars"] <= 0.25 * 900.0


async def test_a_short_is_marked_the_other_way_and_a_gain_is_not_banked(tmp_path):
    g, live, clock = make(tmp_path)
    hold(live, clock, kind=2, size=1, avg=5000.0, last=5010.0)      # short from 5000, last 5010: -40t x $1.25
    await g.mark_positions()
    assert g.open_pnl() == -50.0
    hold(live, clock, kind=2, size=1, avg=5000.0, last=4980.0)      # now +$100 open: the room does NOT grow
    assert g.open_pnl() == 100.0 and g.draw() == 0.0 and g.budget(NOW) == (1000.0, 1000.0)


async def test_draw_is_the_worse_of_committed_stop_risk_and_the_open_loss(tmp_path):
    g, live, clock = make(tmp_path)
    hold(live, clock)                                                # -$100 showing
    await g.mark_positions()
    g._open_risk = lambda: 60.0                                      # our ticket's stop would lose $60: the $100 already showing is worse
    assert g.draw() == 100.0
    g._open_risk = lambda: 150.0                                     # stop risk beyond the loss showing: the stop binds
    assert g.draw() == 150.0 and g.budget(NOW) == (850.0, 850.0)


@pytest.mark.parametrize("spoil", ["no_quote", "stale_quote", "bad_type", "no_avg", "no_record"])
async def test_an_unvaluable_position_makes_the_room_unknown_and_refuses_opens(tmp_path, spoil):
    g, live, clock = make(tmp_path, max_qty_symbol=5, max_qty_total=5)
    hold(live, clock, quote=spoil != "no_quote", age=31.0 if spoil == "stale_quote" else 0.0,
         kind=3 if spoil == "bad_type" else 1)
    if spoil == "no_avg":
        del live.positions[1]["averagePrice"]
    if spoil == "no_record":
        rec = (await g._contract(CID))

        async def lookup(cid):                              # the broker cannot describe the held contract: it cannot be valued
            if cid == H27:
                raise RuntimeError("down")
            return rec
        g._lookup = lookup
    await g.mark_positions()
    assert g.open_pnl() is None and g.draw() is None and g.budget(NOW) == (None, None)
    p = await prev(g)
    assert not p.ok and "open_pnl_known" in failed(p) and "budget_remaining" not in failed(p)


async def test_flat_desk_is_unchanged(tmp_path):
    g, live, clock = make(tmp_path)
    assert g.open_pnl() == 0.0 and g.draw() == 0.0 and g.budget(NOW) == (1000.0, 1000.0)
    p = await prev(g)
    assert p.ok and ("open_pnl_known", True, "") in p.verdict


def test_digest_carries_open_pnl_and_stream_mode_hides_it(tmp_path):
    app, g, live, gw = make_app(tmp_path)
    with TestClient(app) as c:
        app.state.live = g.live
        hold(live, g.clock, avg=5000.0, last=4998.0, size=1)                           # -8t x $1.25 = -$10
        text = c.get("/ai/digest", params={"contract": CID}).text
        st = c.get("/ai/state", params={"contract": CID}).json()
        stream = c.get("/ai/digest", params={"contract": CID, "stream": 1}).text
        sjs = c.get("/ai/state", params={"contract": CID, "stream": 1}).json()
    assert st["risk"]["open_pnl"] == -10.0 and st["risk"]["draw"] == 10.0
    assert "open_pnl=-10 draw=10" in text
    assert "open_pnl" not in stream and sjs["risk"]["open_pnl"] is None and sjs["risk"]["draw"] is None


# ---------------------------------------------------------------------------------- cost as a share of 1R

def test_trade_cost_math_and_unknown_commission_is_na_not_zero():
    lim = Limits(cost_rt_usd=1.0, slippage_ticks=2)
    c = D.trade_cost(lim, 1.25, 18.75)                  # the reviewer's MES example: 15t stop, $3.50 all-in
    assert c == {"cost_usd": 3.5, "cost_pct_r": 18.7, "breakeven_1r_net": 59.3, "fees_rt_usd": 1.0, "slippage_ticks": 2}
    assert D.trade_cost(Limits(cost_rt_usd=0.0), 1.25, 18.75)["cost_pct_r"] is None               # unset: unknown
    assert D.trade_cost(Limits(cost_rt_usd=1.0, slippage_ticks=0), 0.5, 10.0)["cost_usd"] == 1.0  # zero slippage is allowed
    assert D.trade_cost(lim, 1.25, 0.0)["cost_pct_r"] is None
    assert D.trade_cost(lim, "x", 18.75)["cost_pct_r"] is None


def test_size_cost_line_in_digest_and_stream_redaction():
    z = {"cost_usd": 3.5, "cost_pct_r": 18.7, "breakeven_1r_net": 59.3, "fees_rt_usd": 1.0, "slippage_ticks": 2}
    s = state()
    s["size"] = {**s["size"], **z}
    t = D.format_digest(s)
    assert "SIZE_COST: pct_of_1r=18.7% breakeven_1r_net=59.3% per_contract_usd=3.5 fees_rt_usd=1 slippage_t=2" in t
    st = D.format_digest(D.redact_stream(s))
    line = next(l for l in st.splitlines() if l.startswith("SIZE_COST:"))
    assert line == "SIZE_COST: pct_of_1r=18.7% breakeven_1r_net=59.3% slippage_t=2"
    s["size"] = {**s["size"], "cost_usd": None, "cost_pct_r": None, "breakeven_1r_net": None, "fees_rt_usd": None}
    assert "SIZE_COST: pct_of_1r=NA% breakeven_1r_net=NA% per_contract_usd=NA fees_rt_usd=NA slippage_t=2 (set PX_COST_RT_USD)" in D.format_digest(s)


def test_size_box_carries_cost_end_to_end(tmp_path):
    from test_px_sizing_display import prime
    app, g, live, gw = make_app(tmp_path, cost_rt_usd=0.84, slippage_ticks=2)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=2.0)
        st = c.get("/ai/state", params={"contract": CID, "risk_pct": 0.05}).json()
        text = c.get("/ai/digest", params={"contract": CID, "risk_pct": 0.05}).text
        stream = c.get("/ai/digest", params={"contract": CID, "risk_pct": 0.05, "stream": 1}).text
    z = st["size"]
    assert z["status"] == "OK" and z["cost_usd"] == pytest.approx(0.84 + 2 * 1.25)
    assert z["cost_pct_r"] == pytest.approx(100.0 * z["cost_usd"] / z["rpc"], abs=0.1)
    assert z["breakeven_1r_net"] == pytest.approx(100.0 * (z["rpc"] + z["cost_usd"]) / (2 * z["rpc"]), abs=0.1)
    assert any(w.startswith("Cost: $3.34 per MES round turn (2t slippage + $0.84 fees)") for w in z["why"])
    assert "SIZE_COST: pct_of_1r=" in text and "fees_rt_usd=0.84" in text
    assert "SIZE_COST: pct_of_1r=" in stream and "fees_rt_usd" not in stream and "$3.34" not in stream


def test_kpi_cost_line_reports_fees_today(tmp_path):
    k = {"n": 3, "payoff": 1.0, "win_rate": 66.7, "breakeven_win_rate": 50.0, "profit_factor": 2.0, "expectancy": 10.0,
         "fees": 4.2, "fees_pct_gross": 3.5}
    t = D.format_digest(state(kpi=k))
    assert "KPI_COST: fees_pct_of_gross=3.5% fees_today=4.2" in t
    assert "KPI_COST: fees_pct_of_gross=3.5%\n" in D.format_digest(D.redact_stream(state(kpi=k)))


# ---------------------------------------------------------------------------------- config defaults

def test_env_defaults_scalers_off_cost_and_stage(monkeypatch):
    for k in ("PX_SIZE_SCALERS", "PX_COST_RT_USD", "PX_SLIPPAGE_TICKS", "PX_FEED_STAGE"):
        monkeypatch.delenv(k, raising=False)
    lim = Limits.from_env()
    assert lim.size_scalers is False and lim.cost_rt_usd == 0.0 and lim.slippage_ticks == 2 and lim.feed_stage is False
    monkeypatch.setenv("PX_SIZE_SCALERS", "1")
    monkeypatch.setenv("PX_COST_RT_USD", "0.84")
    monkeypatch.setenv("PX_SLIPPAGE_TICKS", "0")
    monkeypatch.setenv("PX_FEED_STAGE", "1")
    lim = Limits.from_env()
    assert lim.size_scalers is True and lim.cost_rt_usd == 0.84 and lim.slippage_ticks == 0 and lim.feed_stage is True
    for bad in ("x", "nan", "inf", "-1", ""):
        monkeypatch.setenv("PX_COST_RT_USD", bad)
        monkeypatch.setenv("PX_SLIPPAGE_TICKS", bad)
        lim = Limits.from_env()
        assert lim.cost_rt_usd == 0.0 and lim.slippage_ticks == 2                 # garbled cost: unknown; garbled slippage: the default
    monkeypatch.setenv("PX_SIZE_SCALERS", "0")
    assert Limits.from_env().size_scalers is False


# ---------------------------------------------------------------------------------- callout STAGE kill switch

def _app(feed_stage):
    st = types.SimpleNamespace(guard=types.SimpleNamespace(limits=Limits(feed_stage=feed_stage)))
    return types.SimpleNamespace(state=st)


def test_feed_result_says_whether_one_click_stage_is_on():
    env = {"PX_FEED_URL": "https://bot.example/analyst-callouts", "PX_FEED_KEY": "k"}

    async def ok(url, key, hours, now):
        return []

    off = asyncio.run(px_feed.feed_result(_app(False), env, fetch=ok))
    on = asyncio.run(px_feed.feed_result(_app(True), env, fetch=ok))
    assert off["status"] == "OK" and off["stage"] is False and on["stage"] is True
    assert asyncio.run(px_feed.feed_result(_app(True), {}))["stage"] is True                 # the flag is reported even when the feed is OFF
    assert asyncio.run(px_feed.feed_result(types.SimpleNamespace(state=types.SimpleNamespace()), {}))["stage"] is False
    item = {"ts": "2026-10-17T14:00:00Z", "age_s": 60, "channel": "futures", "analyst": "a", "symbol": "MNQ", "side": "LONG", "event": "ENTRY",
            "entry": 1.0, "stop": 0.5, "tp1": None, "tp2": None, "tp3": None, "points": None}
    assert px_feed.feed_lines({"status": "OK", "items": [item], "stage": True})[-1].endswith("one_click_stage=ON")
    assert px_feed.feed_lines({"status": "OK", "items": [item]})[-1].endswith("one_click_stage=OFF")


def test_callout_sourced_tickets_are_audited(tmp_path):
    app, g, live, gw = make_app(tmp_path)
    with TestClient(app) as c:
        app.state.live = g.live
        tok = c.get("/api/csrf").json()["token"]
        r = post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_dollars": 50, "source": "callout"})
        assert r.json()["ok"]
        r2 = post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_dollars": 50, "source": "<script>"})
        assert r2.json()["ok"]
        post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_dollars": 50})
    events = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    src = [e for e in events if e["event"] == "stage_source"]
    assert len(src) == 1 and src[0]["code"] == "callout" and src[0]["origin"] == "ui" and len(src[0]["ticket"]) == 8
    assert "<script>" not in (tmp_path / "audit.jsonl").read_text()


# ---------------------------------------------------------------------------------- respect: the three gates

def test_respected_needs_the_fdr_pass_not_just_the_gap():
    rows = [{"verdict": R.RESPECTED, "p_value": 0.04}] + [{"verdict": "NOT_DISTINGUISHABLE", "p_value": 0.5} for _ in range(24)]
    fam = R._decide([rows])
    assert fam == 25 and rows[0]["verdict"] == "NOT_DISTINGUISHABLE" and rows[0]["fdr"] is False       # 0.04 alone is not enough against 24 siblings
    rows = [{"verdict": R.RESPECTED, "p_value": 0.0005}] + [{"verdict": "TOO_FEW_TESTS", "p_value": None} for _ in range(24)]
    assert R._decide([rows]) == 1 and rows[0]["verdict"] == R.RESPECTED and rows[0]["fdr"] is True and rows[1]["fdr"] is None
    rows = [{"verdict": "BELOW_RANDOM", "p_value": 0.99}]
    R._decide([rows])
    assert rows[0]["verdict"] == "BELOW_RANDOM" and rows[0]["fdr"] is False                           # only RESPECTED is downgraded


def test_scan_uses_one_family_across_timeframes():
    from test_px_respect import ema_bouncer, walk
    r = R.scan({"1h": ema_bouncer(), "15m": walk(1000, 1)}, top=99)
    f1, f2 = r["tfs"]["1h"]["family"], r["tfs"]["15m"]["family"]
    hyps = sum(1 for t in r["tfs"].values() for x in t["levels"] if x["p_value"] is not None)       # a row with no p-value is not a hypothesis
    assert f1 == f2 == hyps > max(r["tfs"]["1h"]["tested"], r["tfs"]["15m"]["tested"])
    alone = R.scan_tf(ema_bouncer(), "1h", top=99)
    assert alone["family"] <= alone["tested"] < f1
    row = next(x for x in r["tfs"]["1h"]["levels"] if x["id"] == "ema34")
    assert row["verdict"] == R.RESPECTED and row["fdr"] is True and row["p_value"] < 0.01 and row["tests"] >= R.MIN_N


def test_block_surrogate_keeps_runs_of_bar_shapes_together():
    bars = R.normalize([{"o": i, "h": i + 1.5, "l": i - 0.5, "c": i + 1, "v": 1, "t": 1_700_000_000_000 + i * 60000} for i in range(200)])
    sb = R.surrogate(bars, seed=1, block=20)
    assert len(sb) == 200 and [b["t"] for b in sb] == [b["t"] for b in bars]
    # every bar shape in the original is "open at the prior close, close 1 higher": the surrogate keeps each shape exactly
    shapes = {(round(b["o"] - a["c"], 9), round(b["c"] - a["c"], 9)) for a, b in zip(sb, sb[1:])}
    assert shapes == {(0.0, 1.0)}
    from test_px_respect import walk
    w = R.normalize(walk(200, 1))
    a, b = R.surrogate(w, 1), R.surrogate(w, 2)
    assert a == R.surrogate(w, 1) and a != b and a != w                                            # deterministic per seed, and not the original
    # blocks: with block=20 at least one run of 20 consecutive original shapes survives intact
    orig = [(round(y["c"] - x["c"], 9)) for x, y in zip(w, w[1:])]
    got = [(round(y["c"] - x["c"], 9)) for x, y in zip(a, a[1:])]
    assert any(got[i:i + 20] == orig[j:j + 20] for i in range(0, len(got) - 20, 1) for j in range(len(orig) - 20))
    assert R.surrogate(w[:1], 3) == w[:1]
