"""A trailing stop (ProjectX order type 5) is protection under the same conditions as a STOP (type 4). The desk never modifies it,
never places one, and every cancel sweep removes it."""
import pytest

from projectx_desk.guard import ORDER_CANCEL, ORDER_MODIFY, ORDER_PLACE
from projectx_desk.state import is_protective_stop
from test_px_guard_place import CID
from test_px_guard_protect import build2, fill

TRAIL = {"id": 950, "accountId": 7, "contractId": CID, "type": 5, "side": 1, "size": 1, "stopPrice": 4990.0,
         "limitPrice": None, "trailPrice": 4.5, "status": 1}
STOP = {**TRAIL, "id": 951, "type": 4, "trailPrice": None, "stopPrice": 4985.0}


def test_helper_trail_covers_only_when_working_on_the_closing_side_big_enough_and_readable():
    assert is_protective_stop(TRAIL, "long", 1, CID) and is_protective_stop(STOP, "long", 1, CID)
    assert is_protective_stop({**TRAIL, "side": 0}, "short", 1)
    assert not is_protective_stop({**TRAIL, "side": 0}, "long", 1)           # wrong side
    assert not is_protective_stop({**TRAIL, "size": 1}, "long", 2)           # too small
    assert not is_protective_stop({**TRAIL, "status": 3}, "long", 1)         # cancelled / not working
    assert not is_protective_stop({**TRAIL, "status": 2}, "long", 1)         # filled
    for bad in (None, "4990", float("nan"), float("inf"), True, 0, -1):
        assert not is_protective_stop({**TRAIL, "stopPrice": bad}, "long", 1)    # no readable trigger
    assert not is_protective_stop({k: v for k, v in TRAIL.items() if k != "stopPrice"}, "long", 1)
    assert not is_protective_stop({**TRAIL, "contractId": "CON.F.US.MNQ.Z26"}, "long", 1, CID)
    assert not is_protective_stop({**TRAIL, "type": 3}, "long", 1)           # STOPLIMIT is not protection
    assert not is_protective_stop(TRAIL, None, 1) and not is_protective_stop(TRAIL, "long", None)
    assert not is_protective_stop("junk", "long", 1)


async def test_protect_open_positions_treats_a_working_trail_as_covered_and_does_not_flatten(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    fill(live)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    gw.working = [dict(TRAIL)]
    assert await g.protect_open_positions() == {CID: "COVERED"}
    assert not g.halted() and not [w for w in gw.writes if w[0] in (ORDER_CANCEL, ORDER_PLACE)]


@pytest.mark.parametrize("bad", [{"side": 0}, {"size": 0}, {"status": 3}, {"stopPrice": None}])
async def test_a_trail_that_does_not_cover_is_not_covered(tmp_path, bad):
    g, live, clock, gw = build2(tmp_path)
    fill(live)
    gw.working = [{**TRAIL, **bad}]
    assert (await g.protect_open_positions()) == {CID: "EXTERNAL_UNPROTECTED"}        # external, reported, never touched
    live.orders = {950: {**TRAIL, **bad}}
    assert g.open_trade(CID) is None


async def test_adopted_trade_with_a_trail_cannot_be_moved(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    live.positions = {1: {"id": 1, "contractId": CID, "type": 1, "size": 1, "averagePrice": 5000.0}}
    live.orders = {950: dict(TRAIL)}
    t = g.open_trade(CID)
    assert t is not None and t.adopted and t.trail and t.stop_current == 4990.0
    live.quotes[CID]["_rx"] = clock()
    r = await g.move_stop(CID, 4995.0)
    assert not r.ok and r.reason.startswith("trail_stop")
    assert not [w for w in gw.writes if w[0] == ORDER_MODIFY]                        # never modified, never replaced
    assert not [w for w in gw.writes if w[0] == ORDER_PLACE]


async def test_a_stop_and_a_trail_together_adopt_nothing(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    live.positions = {1: {"id": 1, "contractId": CID, "type": 1, "size": 1, "averagePrice": 5000.0}}
    live.orders = {950: dict(TRAIL), 951: dict(STOP)}
    assert g.open_trade(CID) is None
    r = await g.move_stop(CID, 4995.0)
    assert not r.ok and not [w for w in gw.writes if w[0] == ORDER_MODIFY]


async def test_a_plain_stop_is_still_adopted_and_not_marked_trail(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    live.positions = {1: {"id": 1, "contractId": CID, "type": 1, "size": 1, "averagePrice": 5000.0}}
    live.orders = {951: dict(STOP)}
    t = g.open_trade(CID)
    assert t is not None and t.adopted and not t.trail


async def test_flatten_cancels_a_resting_trail_so_it_cannot_open_a_reverse_position(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    gw.working = [dict(TRAIL), dict(STOP)]
    r = await g.flatten(contract_id=CID)
    assert r.flat and r.orphan_orders == 0 and gw.working == []
    assert sorted(gw.cancelled) == [950, 951]
    cancels = [i for i, w in enumerate(gw.writes) if w[0] == ORDER_CANCEL]
    close = next(i for i, w in enumerate(gw.writes) if w[0] != ORDER_CANCEL)
    assert cancels and max(cancels) < close                                          # cancelled before the close


async def test_flatten_reports_a_trail_that_will_not_cancel_as_an_orphan(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    gw.working = [dict(TRAIL)]

    async def refuse(path, body):
        gw.writes.append((path, dict(body)))
        return {"success": False} if path == ORDER_CANCEL else {"success": True}
    gw.write = refuse
    r = await g.flatten(contract_id=CID)
    assert not r.flat and r.orphan_orders >= 1 and g.halted()


def test_order_view_labels_a_trail_and_the_digest_shows_it():
    from projectx_desk.state import order_view
    v = order_view(TRAIL)
    assert v["type"] == "TRAIL" and v["stop"] == 4990.0 and v["side"] == "SELL"
