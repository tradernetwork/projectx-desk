"""One protection owner per net position across a cancelled or expired entry (Round 35, adversarial review).

A cancelled limit entry stays PLACED so a fill that won the cancel race still gets its stop. handle_position gives a fill to EVERY
active ticket on the contract, so CANCEL (or the TTL) followed by a market entry armed TWO stops for one position: both fill and the
account is reversed with no protection. Guard._dead_entry now rules out a cancelled entry that never owned a fill or a stop once
another ticket on the contract owns one or a newer entry was placed after it; alone on its contract it still protects its raced fill.
"""
import json

from projectx_desk.client import PXWriteUnknown
from projectx_desk.guard import ORDER_PLACE
from test_px_guard_cancel import resting
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
    assert t1.stop_state is None and not t1.filled_size
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
    assert t1.stop_state is None and not t1.filled_size


async def test_a_quick_reentry_still_gives_the_fill_to_the_newer_ticket_only(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    t2 = await _reenter(g, live, clock, gw, wait=11)             # just past IN_FLIGHT_S
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t2.hash8}-2"
    assert t1.state == "PLACED" and t1.stop_state is None
    gw.writes.clear()
    gw.pos = [dict(live.positions[1])]
    gw.working = [{"id": 900, "contractId": CID, "type": 4, "side": 1, "size": 1, "status": 1, "stopPrice": 4994.0}]
    out = await g.protect_open_positions()                       # the resync judges the same owner: nothing is flattened
    assert out == {CID: "TICKET"} and gw.writes == [] and not g.halted()


async def test_a_fill_carrying_the_cancelled_entrys_order_id_still_arms_one_stop_only(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    t2 = await _reenter(g, live, clock, gw, wait=11)
    live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})   # the cancel lost the race
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{t2.hash8}-2" and t1.stop_state is None   # the newer entry owns it, sized to the position
    gw.writes.clear()
    await g.handle_position(fill(live, size=2))                  # the newer entry fills too: its one stop resizes, nobody arms a second
    assert stops(gw) == [] and t1.stop_state is None and t2.filled_size == 2


async def test_the_race_fill_of_a_lone_cancelled_entry_is_still_protected(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    live.orders.pop(555)
    clock.t += 20
    assert await g.expire_resting_entries() == 0 and t1.state == "PLACED"
    await g.handle_position(fill(live))
    assert len(stops(gw)) == 1 and t1.stop_state == "ACKED"
    clock.t += 999
    await g.expire_resting_entries()
    assert t1.state == "PLACED"


async def test_an_unclear_cancel_of_an_order_that_filled_reports_filled_not_cancelled(tmp_path):
    for err in (PXWriteUnknown("timeout"), "malformed"):
        g, live, clock, gw, t = await resting(tmp_path / str(type(err).__name__))
        gw.orders = []                                            # searchOpen drops FILLED orders too

        async def filled_meanwhile(err=err, live=live):
            live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})
            if isinstance(err, BaseException):
                raise err
            return None                                           # a malformed reply
        gw.reply = filled_meanwhile
        r = await g.cancel_entry(t.ticket_id)
        assert not r.ok and r.code == "filled" and not t.user_cancelled and not t.ttl_cancelled and not g.halted()
        row = [a for a in audit(g) if a["event"] == "cancel"][-1]
        assert row["state"] == "FILLED" and row["code"] == "user"
        assert not any(a["event"] == "cancel" and a.get("state") == "OK" for a in audit(g))
        assert g.entry_status(t) != "cancelled"


async def test_a_fill_that_lands_during_an_unclear_cancel_is_filled_and_protected(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path, b=build2)
    orig = gw.write

    async def w(path, body):
        if path.endswith("/cancel"):
            gw.writes.append((path, dict(body)))
            live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})   # it filled meanwhile
            raise PXWriteUnknown("timeout")
        return await orig(path, body)
    gw.write = w
    r = await g.cancel_entry(t.ticket_id)                         # unclear reply, REST lists nothing (it filled)
    assert r.code == "filled" and not t.user_cancelled
    gw.write = orig
    live.orders.pop(555)
    await g.handle_position(fill(live))
    assert len(stops(gw)) == 1 and t.stop_state == "ACKED" and g.entry_status(t) == "filled"


# ---- a stale owner from a missed size-0 event (second review): the next entry still arms its own stop -----------------------

B_STOP = {"id": 901, "contractId": CID, "type": 4, "side": 1, "size": 1, "status": 1, "stopPrice": 4994.0}


async def _stale_a_then_b(tmp_path, stop_fill_seen=True):
    """A fills and its stop is ACKED; the stop fills while the user hub is down (no size-0 event, the snapshot just drops the position);
    B is let through by contract_flat. Returns before B's fill is handled."""
    g, live, clock, gw = build2(tmp_path)
    gw.reply = {"success": True, "orderId": 555}
    a = await staged(g)
    assert (await go(g, a)).ok
    await g.handle_position(fill(live))
    assert a.stop_state == "ACKED" and a.filled_size == 1
    live.positions.clear()
    # The Broker stub now lists every stop it ACKs (as a real gateway does), so a filled stop must be removed explicitly: this encodes the
    # truth that a filled stop is no longer listed.
    gw.working = []
    if stop_fill_seen:
        live.trades.append({"id": 50, "orderId": a.stop_order_id, "contractId": CID, "size": 1, "voided": False})
    clock.t += 60
    live.quotes[CID]["_rx"] = clock.t
    gw.reply = {"success": True, "orderId": 556}
    gw.stop_reply = {"success": True, "orderId": 901}
    b = await staged(g)
    assert (await go(g, b)).ok
    gw.writes.clear()
    return g, live, clock, gw, a, b


async def test_stale_owner_from_a_missed_close_takes_the_new_entrys_fill(tmp_path):
    g, live, clock, gw, a, b = await _stale_a_then_b(tmp_path)
    await g.handle_position(fill(live))                          # B's fill
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{b.hash8}-2" and b.stop_state == "ACKED"


async def test_a_stale_owner_whose_stop_fill_was_missed_too_does_not_block_the_next_entry(tmp_path):
    g, live, clock, gw, a, b = await _stale_a_then_b(tmp_path, stop_fill_seen=False)
    assert live.positions_seeded and a.stop_order_id not in live.orders
    await g.handle_position(fill(live))
    s = stops(gw)
    assert len(s) == 1 and s[0]["customTag"] == f"px-{b.hash8}-2" and b.stop_state == "ACKED"


async def test_stale_owner_then_reseed_keeps_the_new_owner(tmp_path):
    g, live, clock, gw, a, b = await _stale_a_then_b(tmp_path)
    p = fill(live)
    await g.handle_position(p)
    gw.writes.clear()
    gw.pos, gw.working = [dict(p)], [dict(B_STOP)]
    out = await g.protect_open_positions()
    assert out[CID] in ("TICKET", "COVERED") and gw.writes == [] and not g.halted() and b.stop_state == "ACKED"


# ---- third review: regressions of the larger design that was abandoned (each must stay fixed) ------------------------------

async def test_a_raced_fill_after_the_new_entry_owns_the_position_never_arms_a_second_stop(tmp_path):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t1.ticket_id)).ok
    t2 = await _reenter(g, live, clock, gw, wait=11)
    await g.handle_position(fill(live))
    assert t2.stop_state == "ACKED" and len(stops(gw)) == 1
    assert t2.stop_order_id not in live.orders                   # ACKed, the hub has not listed it yet
    live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})   # t1's raced fill shows up
    gw.writes.clear()
    await g.handle_position(fill(live, size=2))
    assert stops(gw) == [] and t1.stop_state is None and t2.filled_size == 2


async def test_after_a_restart_our_position_under_our_listed_stop_is_left_alone(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.reply = {"success": True, "orderId": 555}
    a = await staged(g)
    assert (await go(g, a)).ok
    await g.handle_position(fill(live))
    row = {"id": a.stop_order_id, "contractId": CID, "type": 4, "side": 1, "size": 1, "status": 1, "stopPrice": 4994.0}
    live.orders[a.stop_order_id] = row
    g.tickets.clear()                                             # restart: tickets are not persisted, intents are
    gw.writes.clear()
    gw.pos, gw.working = [dict(live.positions[1])], [dict(row)]
    assert await g.protect_open_positions() == {CID: "COVERED"}
    await g.handle_position(fill(live))                           # a later hub position event
    assert gw.writes == [] and not g.halted()


async def test_the_resync_never_closes_out_a_ticket_from_a_positions_read(tmp_path):
    g, live, clock, gw, a, b = await _stale_a_then_b(tmp_path)
    reads = []
    orig = gw.open_positions

    async def counted(account_id):
        reads.append(1)
        return await orig(account_id)
    gw.open_positions = counted
    p = fill(live)
    await g.handle_position(p)                                    # B fills and arms
    live.orders[901] = dict(B_STOP)
    gw.writes.clear()
    gw.pos, gw.working = [dict(p)], [dict(B_STOP)]
    await g.protect_open_positions()
    assert reads == [] and b.state == "PLACED" and b.stop_state == "ACKED" and gw.writes == [] and not g.halted()


# ---- pre-push review: the newer entry fills while PLACING and its POST comes back rejected with an order id -----------------

async def _b_fills_then_rejected(tmp_path, ttl):
    g, live, clock, gw, t1 = await resting(tmp_path, b=build2)
    if ttl:
        clock.t += 400
        live.quotes[CID]["_rx"] = clock.t
        await g.expire_resting_entries()
        assert t1.ttl_cancelled
    else:
        assert (await g.cancel_entry(t1.ticket_id)).ok
    live.orders.pop(555, None)
    clock.t += 11
    live.quotes[CID]["_rx"] = clock.t
    t2 = await staged(g)

    async def reply():                       # t2's fill reaches the hub while its POST is in flight, then the POST says rejected
        await g.handle_position(fill(live))
        return {"success": False, "orderId": 556, "errorCode": 2}
    gw.reply = reply
    gw.writes.clear()
    await go(g, t2)
    assert t2.state == "REJECTED" and t2.stop_state == "ACKED"
    await g.handle_position(fill(live))      # any later position event or reseed
    return g, gw, t1, t2


async def test_human_cancel_then_a_rejected_but_filled_entry_keeps_one_stop(tmp_path):
    g, gw, t1, t2 = await _b_fills_then_rejected(tmp_path, ttl=False)
    assert len(stops(gw)) == 1 and t1.stop_state is None


async def test_ttl_cancel_then_a_rejected_but_filled_entry_keeps_one_stop(tmp_path):
    g, gw, t1, t2 = await _b_fills_then_rejected(tmp_path, ttl=True)
    assert len(stops(gw)) == 1 and t1.stop_state is None
