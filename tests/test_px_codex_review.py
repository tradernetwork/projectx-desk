"""Regression tests for the Codex second-opinion review (reviews/review-codex-2026-10-16.md), one per verified finding."""
import asyncio

from test_px_guard_place import CID, go, staged
from test_px_guard_protect import build2, fill

from px_flow import MIN_MS, FlowStore, summary


async def test_missing_stop_on_our_unchanged_position_flattens_and_halts(tmp_path):         # finding 1
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    p = fill(live)
    gw.pos = [p]
    await g.handle_position(p)
    assert t.stop_state == "ACKED" and t.position_id == 1
    gw.working = []                                         # the stop vanished; the SAME broker position is still open
    out = await g.protect_open_positions()
    assert out[CID] == "FLATTENED" and g.halted()           # ours and unprotected: never relabelled external


async def test_missing_stop_whose_fill_is_on_the_tape_means_the_position_is_new_and_external(tmp_path):   # finding 1, evidence
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    live.positions.clear()
    live.positions[2] = {"id": 2, "accountId": 7, "contractId": CID, "type": 1, "size": 1, "averagePrice": 5000.25}
    live.trades.append({"id": 77, "orderId": t.stop_order_id, "contractId": CID, "voided": False})
    gw.working = []
    n = len(gw.writes)
    assert (await g.protect_open_positions())[CID] == "EXTERNAL_UNPROTECTED"
    assert len(gw.writes) == n and not g.halted()           # our stop provably filled: a different position is never touched


async def test_a_new_position_id_without_a_stop_fill_is_still_ours_and_flattened(tmp_path):       # review: id alone is no evidence
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    live.positions.clear()
    live.positions[2] = {"id": 2, "accountId": 7, "contractId": CID, "type": 1, "size": 1, "averagePrice": 5000.25}
    gw.pos, gw.working = [live.positions[2]], []              # no stop fill anywhere
    assert (await g.protect_open_positions())[CID] == "FLATTENED" and g.halted()


async def test_a_stop_the_trader_replaced_covers_the_position_and_is_left_alone(tmp_path):   # review: no false exit
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    p = fill(live)
    await g.handle_position(p)
    gw.pos = [p]
    gw.working = [{"id": 901, "contractId": CID, "type": 4, "side": 1, "size": 1}]       # their own covering stop, not ours (900)
    n = len(gw.writes)
    assert (await g.protect_open_positions())[CID] == "COVERED"
    assert len(gw.writes) == n and not g.halted()


async def _late_stop_rig(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    release = asyncio.Event()

    async def delayed_stop(path, body):
        gw.writes.append((path, dict(body)))
        if "stopPrice" in body and body.get("type") == 4:
            await release.wait()
            return {"success": True, "orderId": 900}
        return {"success": True}

    gw.write = delayed_stop

    async def long_sleep(_):
        await asyncio.sleep(3600)

    g.sleep = long_sleep
    task = asyncio.create_task(g.handle_position(fill(live)))
    await asyncio.sleep(0)
    return g, live, gw, t, release, task


async def test_stop_ack_after_an_ordinary_close_is_cancelled_and_confirmed_without_a_halt(tmp_path):     # finding 2
    g, live, gw, t, release, task = await _late_stop_rig(tmp_path)
    live.positions.clear()
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    release.set()
    await task
    assert t.state == "CLOSED" and t.abandoned and t.stop_state == "ABANDONED"
    assert any("cancel" in path.lower() for path, _ in gw.writes)       # the late stop was cancelled
    assert not g.halted()                                               # and the book confirms it: a normal close is not an incident


async def test_a_late_stop_that_cannot_be_cancelled_is_an_orphan_and_halts(tmp_path):
    g, live, gw, t, release, task = await _late_stop_rig(tmp_path)
    live.positions.clear()
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    gw.working = [{"id": 900, "contractId": CID, "type": 4, "side": 1, "size": 1}]
    orig = gw.write

    async def refuse_cancel(path, body):
        if "cancel" in path.lower():
            gw.writes.append((path, dict(body)))
            return {"success": False}
        return await orig(path, body)

    gw.write = refuse_cancel
    release.set()
    await task
    assert g.halted() and g._intents[f"px-{t.hash8}-2"]["state"] == "ORPHAN"


def _working_stop(live, gw, oid=900):
    o = {"id": oid, "contractId": CID, "type": 4, "side": 1, "size": 1}
    live.orders[oid] = dict(o)
    gw.working = [dict(o)]


async def _acked_trade(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    assert t.stop_state == "ACKED" and t.stop_order_id == 900
    live.positions.clear()
    return g, live, gw, t


async def test_a_normal_stop_out_makes_no_broker_call_and_no_halt(tmp_path):            # review: the stop's own fill is proof
    g, live, gw, t = await _acked_trade(tmp_path)
    _working_stop(live, gw)
    live.trades.append({"id": 5, "orderId": 900, "voided": False})
    n = len(gw.writes)
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    assert t.state == "CLOSED" and len(gw.writes) == n and not g.halted()


async def test_a_lagging_book_that_still_lists_a_just_filled_stop_is_retried_not_halted(tmp_path):
    g, live, gw, t = await _acked_trade(tmp_path)
    _working_stop(live, gw)
    reads = []
    orig = gw.open_orders

    async def lagging(aid):
        reads.append(1)
        return await orig(aid) if len(reads) == 1 else []      # the second read shows it gone: it had just filled

    gw.open_orders = lagging
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    assert len(reads) >= 2 and not g.halted() and not any("cancel" in w[0].lower() for w in gw.writes)


async def test_one_failed_book_read_after_a_close_is_retried_not_halted(tmp_path):
    g, live, gw, t = await _acked_trade(tmp_path)
    _working_stop(live, gw)
    n = {"c": 0}
    orig = gw.open_orders

    async def flaky(aid):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("429")
        return []

    gw.open_orders = flaky
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    assert not g.halted()


async def test_a_manual_close_with_our_stop_still_working_is_cleaned_up_without_a_halt(tmp_path):    # review #4
    g, live, gw, t = await _acked_trade(tmp_path)
    _working_stop(live, gw)                                  # the trader closed by hand; our stop 900 is still resting
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    assert 900 in gw.cancelled and not gw.working and not g.halted()


async def test_an_unreadable_book_after_every_retry_halts(tmp_path):
    g, live, gw, t = await _acked_trade(tmp_path)
    _working_stop(live, gw)

    async def dead(aid):
        raise RuntimeError("down")

    gw.open_orders = dead
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    assert g.halted()                                        # cannot prove the book is clear: fail closed


async def test_a_superseded_ticket_keeps_its_state_when_the_earlier_checks_also_refuse(tmp_path):    # review #7
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    gw.working = [{"id": 5, "contractId": CID, "type": 1, "side": 0, "size": 1}]     # desk and broker disagree: refusal reason
    orig = gw.open_positions

    async def during(aid):
        await staged(g)
        return await orig(aid)

    gw.open_positions = during
    r = await go(g, t)
    assert not r.ok and t.state == "SUPERSEDED"


async def test_hub_down_during_the_final_read_refuses_the_entry(tmp_path):                  # finding 3a
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    orig = gw.open_positions

    async def during(aid):
        g.user_hub.up = False
        return await orig(aid)

    gw.open_positions = during
    r = await go(g, t)
    assert not r.ok and not [w for w in gw.writes if "Order/place" in w[0]]


async def test_market_drift_during_the_final_read_refuses_the_entry(tmp_path):              # finding 3b
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    orig = gw.open_positions

    async def during(aid):
        live.quotes[CID].update(bestAsk=5010.25, bestBid=5010.0, lastPrice=5010.0, _rx=clock())
        return await orig(aid)

    gw.open_positions = during
    r = await go(g, t)
    assert not r.ok and any("moved" in x for x in r.reasons) and not [w for w in gw.writes if "Order/place" in w[0]]


async def test_restaging_during_placement_does_not_send_the_superseded_ticket(tmp_path):     # finding 4
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    orig = gw.open_positions

    async def during(aid):
        await staged(g)                                     # a second preview supersedes the first mid-placement
        return await orig(aid)

    gw.open_positions = during
    r = await go(g, t)
    assert not r.ok and t.state == "SUPERSEDED" and not [w for w in gw.writes if "Order/place" in w[0]]


async def test_flatten_rereads_positions_after_cancelling_orders(tmp_path):                  # finding 5
    g, live, clock, gw = build2(tmp_path)
    reads = []

    async def positions(aid):
        reads.append(1)
        return [] if len(reads) == 1 else [dict(id=1, contractId=CID, type=1, size=1)]   # an entry fills while we cancel

    async def orders(aid):
        return []

    gw.open_positions, gw.open_orders = positions, orders
    r = await g.flatten()
    assert len(reads) >= 2 and not r.flat and g.halted()    # never "flat" on a stale empty snapshot


async def test_flatten_with_a_clean_book_is_still_flat(tmp_path):
    g, live, clock, gw = build2(tmp_path)

    async def none(aid):
        return []

    gw.open_positions, gw.open_orders = none, none
    r = await g.flatten()
    assert r.flat and not g.halted()


def test_ongoing_tape_outage_does_not_age_into_a_complete_quiet_window():                   # finding 7
    base = 29_866_666 * MIN_MS
    f = FlowStore()
    for i in range(65):
        f.add(CID, base + i * MIN_MS, 1, 0)
    f.mark_gap(CID, base + 65 * MIN_MS)
    s = summary(f, CID, base + 75 * MIN_MS)
    assert all(w["complete"] is False for w in s["w"].values())
    q = FlowStore()                                         # a healthy quiet tape is still complete zero volume
    for i in range(65):
        q.add(CID, base + i * MIN_MS, 1, 0)
    assert summary(q, CID, base + 75 * MIN_MS)["w"][5] == {"buy": 0, "sell": 0, "unknown": 0, "delta": 0, "complete": True}


# ---- second-round reviews (Fable bug hunt): one test per verified finding ----------------------------------------------------

async def test_an_unrecognised_position_side_on_our_contract_is_flattened_not_ignored(tmp_path):    # Fable A
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    p = fill(live, type_=0)                                  # a position type the desk does not map (Short=2 is unverified)
    gw.pos = [p]
    await g.handle_position(p)
    assert g.halted() and not gw.pos                         # flattened and halted, never left naked behind a "handled" label


async def test_protect_does_not_report_a_ticket_as_handled_when_no_stop_was_armed(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    p = fill(live, type_=0)
    gw.pos = [p]
    out = await g.protect_open_positions()
    assert out[CID] == "FLATTENED" and g.halted()


def test_pct_of_budget_cannot_be_used_to_back_out_the_loss_room_in_stream_mode():                   # Fable B
    import px_digest
    s = {"risk": {"daily_limit": 500.0, "daily_room": 437.0, "dd_buffer": 1900.0, "remaining": 437.0, "pnl_day": -63.0},
         "size": {"pct_budget": 3.5, "risk_usd": 35.0, "contracts": 1, "stop_ticks": 7},
         "ticket": {"readback": {"risk_usd": 35.0, "pct_of_budget": 3.5, "size": 1}}, "kpi": {}}
    r = px_digest.redact_stream(s)
    assert r["size"]["pct_budget"] is None and "pct_of_budget" not in r["ticket"]["readback"]
    assert r["risk"]["daily_used_pct"] % 10 == 0 and r["risk"]["daily_limit"] is None


def test_size_box_stop_price_is_exact_on_a_hundredth_tick_contract():                              # Fable D
    import px_digest
    from px_limits import Limits
    rec = {"tickSize": 0.01, "tickValue": 1.0}
    scale = {"multiplier": 1.0, "reasons": [], "reduced": False, "status": "ok"}
    z = px_digest.size_idea(Limits(), "CON.F.US.MCL.Z26", rec, "long", 70.85, 0.13, 25.0, 0.05, 1.5, 500.0, 20.0, scale, 50)
    assert z["stop_ticks"] == 20 and z["rpc"] == 20.0 and z["contracts"] == 1 and not z["refused"]     # was 21t, $21, REFUSED


def test_size_box_refuses_a_stop_the_guard_would_refuse():                                          # Fable minor
    import dataclasses

    import px_digest
    from px_limits import Limits
    rec = {"tickSize": 0.25, "tickValue": 0.5}
    scale = {"multiplier": 1.0, "reasons": [], "reduced": False, "status": "ok"}
    z = px_digest.size_idea(dataclasses.replace(Limits(), max_stop_ticks=40), "CON.F.US.MNQ.Z26", rec, "long", 17500.0, 35.0,
                            25.0, 0.05, 1.5, 500.0, 300.0, scale, 50)
    assert z["refused"] and z["reason"] == "STOP_DISTANCE" and z["contracts"] == 0


async def test_a_flat_by_of_exactly_the_session_open_is_refused_like_any_garbled_value(tmp_path):    # Fable F
    g, live, clock, gw = build2(tmp_path, flat_by="17:00")
    assert g._flat_by_time() is None
    g2, *_ = build2(tmp_path, flat_by="16:59")
    assert g2._flat_by_time() is not None


async def test_boot_refuses_to_pick_an_arbitrary_account(monkeypatch):                              # Fable E
    from types import SimpleNamespace

    import pytest

    import px_app
    from px_client import PXError

    class C:
        async def accounts(self, only_active):
            return [{"id": 1, "canTrade": True}, {"id": 2, "canTrade": True}]

    app = SimpleNamespace(state=SimpleNamespace(client=C(), live=SimpleNamespace(), user=None,
                                                guard=SimpleNamespace(limits=SimpleNamespace(account_id=None))))
    monkeypatch.delenv("PX_ACCOUNT_ID", raising=False)
    with pytest.raises(PXError):
        await px_app._boot(app)
