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


async def test_missing_stop_with_a_different_broker_position_is_still_external(tmp_path):    # finding 1, evidence both ways
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    live.positions.clear()
    live.positions[2] = {"id": 2, "accountId": 7, "contractId": CID, "type": 1, "size": 1, "averagePrice": 5000.25}
    gw.working = []
    n = len(gw.writes)
    assert (await g.protect_open_positions())[CID] == "EXTERNAL_UNPROTECTED"
    assert len(gw.writes) == n and not g.halted()           # a genuinely different position is never touched


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


async def test_stop_ack_after_an_ordinary_close_is_cancelled_not_left_working(tmp_path):     # finding 2
    g, live, gw, t, release, task = await _late_stop_rig(tmp_path)
    live.positions.clear()
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    release.set()
    await task
    assert t.state == "CLOSED" and t.abandoned and t.stop_state == "ABANDONED"
    assert any("cancel" in path.lower() for path, _ in gw.writes) and g.halted()


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
