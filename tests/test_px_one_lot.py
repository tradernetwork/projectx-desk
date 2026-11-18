"""1-lot combine mode (Round 28): PX_ONE_LOT=1 relaxes the risk-%-sizing gates for ONE manual MES/MNQ contract on a sim account.
The only money ceiling left is the loss room: one contract's stop risk plus slippage must fit inside it. Everything else still runs."""
from datetime import datetime, timedelta, timezone

from projectx_desk.limits import Limits

from test_px_guard_preview import CID, NOW, failed, make


def stop_for(ticks, ask=5000.25, tick=0.25):
    return ask - ticks * tick


async def one(g, ticks, qty=1, side="long"):
    return await g.preview(contract_id=CID, side=side, stop_price=stop_for(ticks), risk_dollars=None, qty=qty)


async def test_wide_stop_is_refused_today_and_placeable_in_one_lot_mode(tmp_path):
    g, *_ = make(tmp_path)
    p = await one(g, 250)                                    # past PX_MAX_STOP_TICKS (200), $312.50 against a 25% cap of $250
    assert not p.ok and "stop_distance" in failed(p)
    g, *_ = make(tmp_path, one_lot=True, one_lot_slip_ticks=8)
    p = await one(g, 250)
    assert p.ok, p.reasons
    assert p.ticket.payload["size"] == 1 and p.ticket.one_lot
    assert ("one_lot_room", True, "") in p.verdict


async def test_the_room_check_counts_slippage(tmp_path):
    # 250 ticks x $1.25 = $312.50, +8 ticks slip = $322.50
    g, *_ = make(tmp_path, one_lot=True, one_lot_slip_ticks=8, daily_loss_limit=322.0)
    p = await one(g, 250)
    assert not p.ok and "one_lot_room" in failed(p)
    g, *_ = make(tmp_path, one_lot=True, one_lot_slip_ticks=8, daily_loss_limit=323.0, dd_buffer=2000.0)
    assert (await one(g, 250)).ok
    g, *_ = make(tmp_path, one_lot=True, one_lot_slip_ticks=0, daily_loss_limit=313.0)
    assert (await one(g, 250)).ok                            # slip 0 is allowed


async def test_one_lot_branch_is_not_taken_for_other_shapes(tmp_path):
    g, *_ = make(tmp_path, one_lot=True, max_qty_order=2, max_qty_symbol=3, max_qty_total=3)
    p = await one(g, 250, qty=2)                             # two contracts: old rules
    assert not p.ok and "stop_distance" in failed(p)
    g, *_ = make(tmp_path, one_lot=True, account_kind="live", allow_live=True, bracket_mode="oco")
    p = await one(g, 250)
    assert not p.ok and "stop_distance" in failed(p)
    g, *_ = make(tmp_path, one_lot=True)
    p = await g.preview(contract_id=CID, side="long", stop_price=stop_for(250), risk_dollars=250.0)   # a risk-sized (non-manual) order
    assert not p.ok


async def test_stop_under_four_ticks_is_still_refused(tmp_path):
    g, *_ = make(tmp_path, one_lot=True)
    p = await one(g, 3)
    assert not p.ok and "stop_distance" in failed(p)


async def test_cooldown_is_off_but_the_loss_lockout_stays(tmp_path):
    lost = [(NOW - timedelta(seconds=60), -50.0)]
    g, live, clock = make(tmp_path, one_lot=True)
    live.closed_trades = lambda now=None: lost
    assert (await one(g, 40)).ok                             # one loss a minute ago: no cooldown in 1-lot mode
    g, live, clock = make(tmp_path)
    live.closed_trades = lambda now=None: lost
    assert "cooldown_after_loss" in failed(await one(g, 40))   # sanity: the rail is on without the mode
    three = [(NOW - timedelta(seconds=600 - i), -50.0) for i in range(3)]
    g, live, clock = make(tmp_path, one_lot=True)
    live.closed_trades = lambda now=None: three
    assert "loss_lockout" in failed(await one(g, 40))


async def test_after_loss_risk_cap_does_not_shrink_a_one_lot_ticket(tmp_path):
    g, live, clock = make(tmp_path, one_lot=True, cooldown_after_loss_s=0)
    live.closed_trades = lambda now=None: [(NOW - timedelta(seconds=900), -10.0)]
    g.day.last_risk_today = lambda now=None: 5.0              # the losing trade risked $5; a 40-tick stop is $50
    assert (await one(g, 40)).ok


async def test_the_other_rails_still_refuse(tmp_path):
    g, *_ = make(tmp_path, one_lot=True, no_new_after="09:00")           # 10:00 CT is past the cutoff
    assert "session_window" in failed(await one(g, 40))
    g, *_ = make(tmp_path, one_lot=True)
    g.trip_halt("unknown_order")
    assert "halt" in failed(await one(g, 40))


def test_limits_parse_the_switch_and_the_slip(monkeypatch):
    monkeypatch.delenv("PX_ONE_LOT", raising=False)
    monkeypatch.delenv("PX_ONE_LOT_SLIP_TICKS", raising=False)
    lim = Limits.from_env()
    assert lim.one_lot is False and lim.one_lot_slip_ticks == 8
    monkeypatch.setenv("PX_ONE_LOT", "1")
    monkeypatch.setenv("PX_ONE_LOT_SLIP_TICKS", "0")
    lim = Limits.from_env()
    assert lim.one_lot is True and lim.one_lot_slip_ticks == 0
    monkeypatch.setenv("PX_ONE_LOT_SLIP_TICKS", "junk")
    assert Limits.from_env().one_lot_slip_ticks == 8         # garbled: default, never silently 0


async def test_a_one_lot_ticket_arms_a_trail_on_the_opposite_side_without_trail_entry(tmp_path):
    from projectx_desk.guard import ORDER_PLACE
    from test_px_guard_place import go, staged
    from test_px_guard_protect import build2, fill
    from test_px_trail_arm import TB

    async def run(**over):
        g, live, clock, _ = build2(tmp_path, **over)
        gw = TB()
        g.client, g.verify_interval = gw, 0
        t = await staged(g, risk_dollars=None, qty=1)
        await go(g, t)
        live.quotes[CID].update(bestBid=5010.0, bestAsk=5010.25, lastPrice=5010.0)
        live.quotes[CID]["_rx"] = clock()
        await g.handle_position(fill(live))
        return g, t, [b for p, b in gw.writes if p == ORDER_PLACE and b.get("type") in (4, 5)]

    g, t, stops = await run(one_lot=True, trading=True)       # PX_TRAIL_ENTRY stays off
    assert t.one_lot and len(stops) == 1
    b = stops[0]
    assert b["type"] == 5 and b["side"] == 1 and b["stopPrice"] is None   # long entry: SELL-side trail
    assert t.trail is True and not g.halted()
    g, t, stops = await run(trading=True)                     # without the mode: the fixed stop, unchanged
    assert not t.one_lot and stops[0]["type"] == 4


def test_digest_stop_cap_is_the_room_in_one_lot_mode():
    from projectx_desk import digest
    lim = lambda **o: Limits(account_kind="sim", daily_loss_limit=1000.0, dd_buffer=2000.0, risk_base="remaining", **o)
    rec = {"tickSize": 0.25, "tickValue": 1.25}
    kw = dict(contract_id=CID, rec=rec, side="long", ref=5000.25, atr_value=40.0, adx_value=25.0, risk_pct=0.1, stop_k=1.5,
              remaining=500.0, chosen=50.0, scale={"multiplier": 1.0, "reasons": [], "reduced": False, "status": "off"}, pctile=50.0)
    off = digest.size_idea(lim(), **kw)
    on = digest.size_idea(lim(one_lot=True, one_lot_slip_ticks=8), **kw)
    assert off["stop_max"] == 200 and off["one_lot"] is False
    assert on["stop_max"] == 500 // 1.25 - 8 == 392 and on["one_lot"] is True       # $500 room / $1.25 a tick, less 8 ticks of slippage
    assert on["stop_ticks"] == off["stop_ticks"] == 240       # the 1.5 x ATR stop is the same; only the ceiling it is held to moved
    assert any("PX_MAX_STOP_TICKS" in w for w in off["why"]) and not any("PX_MAX_STOP_TICKS" in w for w in on["why"])
