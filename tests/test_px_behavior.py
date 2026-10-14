"""Behavioral rails built from a real trader's own history (see docs/DECISIONS.md): refuse or reduce, never allow more."""
from datetime import datetime, timedelta, timezone

import pytest

from test_px_guard_place import GW, build, go, staged
from test_px_guard_preview import CID, NOW, make, prev


def fills(*pnls_and_minutes_ago):
    out = []
    for i, (pnl, mins) in enumerate(pnls_and_minutes_ago):
        ts = (NOW - timedelta(minutes=mins)).isoformat()
        out.append({"id": 100 + i, "accountId": 7, "creationTimestamp": ts, "profitAndLoss": pnl, "fees": 0})
    return out


def failed(p):
    return {n for n, ok, _ in p.verdict if not ok}


async def test_cooldown_after_a_loss_refuses_then_clears_with_time(tmp_path):
    g, live, _ = make(tmp_path, cooldown_after_loss_s=300)
    live.seed_trades(fills((-20.0, 2)))                           # lost 2 minutes ago
    p = await prev(g)
    assert not p.ok and "cooldown_after_loss" in failed(p)
    assert "Walk away" in dict((n, d) for n, ok, d in p.verdict)["cooldown_after_loss"]
    assert g.behavior()["cooldown_left_s"] == 180
    live.seed_trades(fills((-20.0, 6)))                           # lost 6 minutes ago: over
    assert "cooldown_after_loss" not in failed(await prev(g))


async def test_a_win_does_not_trigger_cooldown_and_zero_disables_it(tmp_path):
    g, live, _ = make(tmp_path, cooldown_after_loss_s=300)
    live.seed_trades(fills((+20.0, 1)))
    assert "cooldown_after_loss" not in failed(await prev(g))
    g2, live2, _ = make(tmp_path, cooldown_after_loss_s=0)
    live2.seed_trades(fills((-20.0, 1)))
    assert "cooldown_after_loss" not in failed(await prev(g2))


async def test_consecutive_losses_lock_the_session_and_a_win_resets_the_count(tmp_path):
    g, live, _ = make(tmp_path, cooldown_after_loss_s=0, max_consecutive_losses=3)
    live.seed_trades(fills((-5.0, 90), (-5.0, 60), (-5.0, 30)))
    p = await prev(g)
    assert not p.ok and "loss_lockout" in failed(p) and g.behavior()["consecutive_losses"] == 3
    live.seed_trades(fills((-5.0, 90), (-5.0, 60), (+9.0, 40), (-5.0, 30)))     # a win broke the streak
    assert g.behavior()["consecutive_losses"] == 1 and "loss_lockout" not in failed(await prev(g))
    g2, live2, _ = make(tmp_path, cooldown_after_loss_s=0, max_consecutive_losses=0)
    live2.seed_trades(fills((-5.0, 90), (-5.0, 60), (-5.0, 30), (-5.0, 20)))
    assert "loss_lockout" not in failed(await prev(g2))


async def test_after_a_loss_risk_may_not_exceed_what_the_losing_trade_risked(tmp_path):
    g, live, _ = make(tmp_path, cooldown_after_loss_s=0, max_qty_order=5, max_qty_symbol=5, max_qty_total=5)
    g.day.count_order(NOW, risk=31.25)                            # the last order risked $31.25
    live.seed_trades(fills((-31.0, 20)))
    assert g.behavior()["risk_cap"] == 31.25
    p = await prev(g, risk_dollars=200.0)                          # asks for far more
    assert p.ok and p.sizing["total_risk_dollars"] <= 31.25 and p.sizing["contracts"] == 1
    live.seed_trades(fills((+31.0, 20)))                           # after a WIN the cap is lifted
    assert g.behavior()["risk_cap"] is None
    p2 = await prev(g, risk_dollars=200.0)
    assert p2.ok and p2.sizing["contracts"] > 1


async def test_the_lock_can_be_turned_off_and_unknown_last_risk_applies_no_cap(tmp_path):
    g, live, _ = make(tmp_path, cooldown_after_loss_s=0, size_after_loss_lock=False)
    g.day.count_order(NOW, risk=10.0)
    live.seed_trades(fills((-31.0, 20)))
    assert g.behavior()["risk_cap"] is None
    g2, live2, _ = make(tmp_path, cooldown_after_loss_s=0)
    live2.seed_trades(fills((-31.0, 20)))
    g2.day.last_risk = None
    assert g2.behavior()["risk_cap"] is None                       # a loss from before the desk: nothing to cap against


async def test_last_risk_survives_a_restart_and_resets_on_a_new_trade_date(tmp_path):
    g, live, _ = make(tmp_path)
    g.day.count_order(NOW, risk=25.0)
    from px_limits import DayState
    again = DayState(g.limits.state_path)
    assert again.last_risk_today(NOW) == 25.0
    assert again.last_risk_today(NOW + timedelta(days=1)) is None
    again.count_order(NOW + timedelta(days=1))
    assert again.last_risk is None and again.orders == 1


async def test_behavior_is_unknown_not_clear_when_trades_are_not_seeded(tmp_path):
    g, live, _ = make(tmp_path, seed=False)
    assert g.behavior()["known"] is False
    p = await prev(g)
    assert not p.ok and "day_pnl_known" in failed(p)


async def test_place_rechecks_the_rails_so_a_loss_between_stage_and_place_blocks(tmp_path):
    g, live, clock, gw = build(tmp_path, cooldown_after_loss_s=300)
    t = await staged(g)
    live.seed_trades(fills((-12.0, 0.5)))                          # a loss lands after staging
    r = await go(g, t)
    assert not r.ok and any("cooldown_after_loss" in x for x in r.reasons) and gw.writes == []


def test_place_records_the_risk_of_the_order_for_the_size_lock(tmp_path):
    import asyncio
    g, live, clock, gw = build(tmp_path)
    async def run():
        t = await staged(g)
        await go(g, t)
        return t
    t = asyncio.run(run())
    assert g.day.last_risk == t.readback["risk_usd"] > 0


def test_payoff_behavior_and_size_lock_reach_the_ai_surface(tmp_path):
    from fastapi.testclient import TestClient
    from test_px_app_actions import make_app
    from test_px_sizing_display import RECS, bars_newest_first
    app, g, live, gw = make_app(tmp_path, size_scalers=False, cooldown_after_loss_s=0)
    with TestClient(app) as c:
        app.state.live = g.live

        async def lookup(cid):
            return RECS[cid]

        async def bars(body):
            return bars_newest_first(atr=0.8)

        g._lookup = lookup
        app.state.client.bars = bars
        g._contracts.clear()
        now = datetime.now(timezone.utc)
        iso = lambda m: (now - timedelta(minutes=m)).isoformat()
        live.quotes["CON.F.US.MNQ.Z26"] = {"bestAsk": 17500.5, "bestBid": 17500.25, "lastPrice": 17500.25, "_rx": g.clock()}
        live.seed_trades([{"id": 1, "accountId": 7, "creationTimestamp": iso(50), "profitAndLoss": 10.0, "fees": 0},
                          {"id": 2, "accountId": 7, "creationTimestamp": iso(40), "profitAndLoss": 10.0, "fees": 0},
                          {"id": 3, "accountId": 7, "creationTimestamp": iso(30), "profitAndLoss": -40.0, "fees": 0}])
        g.day.count_order(now, risk=3.0)
        s = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).json()
        assert s["kpi"]["payoff"] == 0.25 and s["kpi"]["breakeven_win_rate"] == 80.0
        assert s["behavior"] == {"known": True, "consecutive_losses": 1, "cooldown_left_s": 0, "lockout_after": 3, "locked": False, "size_locked": True}
        z = s["size"]
        assert z["loss_capped"] and z["budget_usd"] == 3.0 and any("No sizing up to chase" in w for w in z["why"])
        text = c.get("/ai/digest", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05}).text
        assert "payoff=0.25" in text and "BEHAVIOR: consecutive_losses=1 cooldown_s=0 locked=NO size_locked=YES" in text
        st = c.get("/ai/state", params={"contract": "CON.F.US.MNQ.Z26", "risk_pct": 0.05, "stream": 1}).json()
        assert "$" not in " ".join(st["size"]["why"]) and "risk_cap" not in str(st["behavior"])
