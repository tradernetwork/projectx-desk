"""One protection owner per net position, also across a cancelled, expired or gone entry (Round 35, adversarial review).

A cancelled limit entry stays PLACED so a fill that won the cancel race still gets its stop. handle_position used to give a fill to
EVERY active ticket on the contract, so CANCEL (or the TTL) followed by a market entry armed TWO stops for one position: both fill and
the account is reversed with no protection. A position now goes to exactly one ticket (Guard._position_owner): the one that already
owns it, else the one whose entry order id the fills carry, else the newest. A cancelled entry the broker shows gone and unfilled is
CLOSED after CANCEL_GRACE_S and can never own a fill again.
"""
import json

from projectx_desk.guard import ORDER_PLACE
from test_px_guard_cancel import _row, resting
from test_px_guard_place import CID, go, staged
from test_px_guard_protect import build2, fill


def stops(gw):
    return [b for p, b in gw.writes if p == ORDER_PLACE and b["type"] == 4]


def audit(g):
    return [json.loads(x) for x in g.limits.audit_path.read_text(encoding="utf-8").splitlines()]


async def _reenter(g, live, clock, gw, wait=60):
    live.orders.pop(555, None)           # hub: the cancelled entry is gone
    clock.t += wait                      # past IN_FLIGHT_S: contract_flat lets a new entry through
    live.quotes[CID]["_rx"] = clock.t
    gw.reply = {"success": True, "orderId": 556}
    t2 = await staged(g)                 # a NEW market entry, same contract
    assert (await go(g, t2)).ok
    gw.writes.clear()
    return t2


async def test_human_cancel_then_market_reentry_arms_exactly_one_stop_owned_by_the_new_ticket(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    t2 = await _reenter(g, live, clock, gw)
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t2.hash8}-2" and s[0]["size"] == 1
    assert t2.stop_state == "ACKED" and t2.filled_size == 1
    assert t1.stop_state is None and not t1.filled_size and t1.state == "CLOSED"     # past the grace, gone, unfilled: retired
    await g.handle_position(fill(live, size=2))                                        # a resize goes to the same owner only
    assert t1.stop_state is None and t2.filled_size == 2


async def test_ttl_cancel_then_reentry_arms_exactly_one_stop(tmp_path):
    g, live, clock, gw = build2(tmp_path, limit_ttl_s=300)
    gw.reply = {"success": True, "orderId": 555}
    t1 = await staged(g, order_type="limit", entry=4999.0)
    assert (await go(g, t1)).ok
    live.orders[555] = {"id": 555, "contractId": CID, "status": 1, "type": 1, "side": 0, "size": 1}
    clock.t += 999
    assert await g.expire_resting_entries() == 1 and t1.ttl_cancelled
    t2 = await _reenter(g, live, clock, gw)
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t2.hash8}-2"
    assert t1.stop_state is None and t1.state == "CLOSED"
    assert [a for a in audit(g) if a["event"] == "cancel" and a.get("state") == "RETIRED"][-1]["code"] == "limit_ttl"


async def test_reentry_inside_the_grace_still_gives_the_fill_to_the_newest_ticket_only(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    t2 = await _reenter(g, live, clock, gw, wait=11)             # past IN_FLIGHT_S, inside CANCEL_GRACE_S
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t2.hash8}-2"
    assert t1.state == "PLACED" and t1.stop_state is None
    gw.writes.clear()
    gw.pos = [dict(live.positions[1])]
    gw.working = [{"id": 900, "contractId": CID, "type": 4, "side": 1, "size": 1, "status": 1, "stopPrice": 4994.0}]
    out = await g.protect_open_positions()                       # the resync judges the same owner: nothing is flattened
    assert out == {CID: "TICKET"} and gw.writes == [] and not g.halted()


async def test_a_gone_entry_does_not_own_the_next_entrys_fill(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.reply = {"success": True, "orderId": 555}
    t1 = await staged(g)
    assert (await go(g, t1)).ok
    clock.t += 60                                                 # never listed, no fill: entry_status gone
    assert g.entry_status(t1) == "gone"
    live.quotes[CID]["_rx"] = clock.t
    gw.reply = {"success": True, "orderId": 556}
    t2 = await staged(g)
    assert (await go(g, t2)).ok
    gw.writes.clear()
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t2.hash8}-2" and t1.stop_state is None


async def test_a_fill_carrying_the_older_entrys_order_id_goes_to_that_ticket_only(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    t2 = await _reenter(g, live, clock, gw, wait=11)
    live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})   # the cancel lost the race
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t1.hash8}-2" and t2.stop_state is None and t1.state == "PLACED"


async def test_the_race_fill_inside_the_grace_is_still_protected(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    live.orders.pop(555)
    clock.t += 20
    assert await g.expire_resting_entries() == 0 and t1.state == "PLACED"      # inside the grace: not retired
    await g.handle_position(fill(live))
    assert len(stops(gw)) == 1 and t1.stop_state == "ACKED"
    clock.t += 999
    await g.expire_resting_entries()
    assert t1.state == "PLACED"                                   # it owns a fill: never retired


async def test_a_retired_entry_never_arms_a_stop_and_retirement_needs_all_conditions(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    clock.t += 999
    await g.expire_resting_entries()
    assert t1.state == "PLACED"                                   # the broker still lists it working
    live.orders[555] = _row(t1, status=3)                         # listed, but no longer working
    live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})
    await g.expire_resting_entries()
    assert t1.state == "PLACED"                                   # a fill carries its order id
    live.trades.clear()
    await g.expire_resting_entries()
    assert t1.state == "CLOSED" and g.entry_status(t1) is None
    gw.writes.clear()
    await g.handle_position(fill(live))
    assert stops(gw) == [] and t1.stop_state is None
