"""PX_TRAIL_ENTRY: the entry's post-fill protective stop is placed as a TRAIL (type 5). Default OFF: type 4, unchanged."""
from projectx_desk.guard import ORDER_PLACE
from test_px_guard_place import go, staged
from test_px_guard_preview import CID
from test_px_guard_protect import build2, fill
from test_px_trail_arm import TB


async def run_entry(tmp_path, fresh_quote=True, **over):
    g, live, clock, _ = build2(tmp_path, **over)
    gw = TB()
    g.client, g.verify_interval = gw, 0
    t = await staged(g)
    await go(g, t)
    live.quotes[CID].update(bestBid=5010.0, bestAsk=5010.25, lastPrice=5010.0)
    live.quotes[CID]["_rx"] = clock() if fresh_quote else clock() - 3600
    await g.handle_position(fill(live))
    return g, t, gw, [b for p, b in gw.writes if p == ORDER_PLACE and b.get("type") in (4, 5)]


async def test_off_sends_a_fixed_stop(tmp_path):
    g, t, gw, stops = await run_entry(tmp_path)
    assert len(stops) == 1
    b = stops[0]
    assert b["type"] == 4 and b["stopPrice"] == t.stop_plan["stop_price"] and b["trailPrice"] is None
    assert t.trail is False


async def test_on_sends_a_trail_at_an_absolute_level_on_a_tick(tmp_path):
    g, t, gw, stops = await run_entry(tmp_path, trail_entry=True)
    assert len(stops) == 1
    b = stops[0]
    d = abs(t.readback["entry_ref"] - t.stop_plan["stop_price"])
    assert b["type"] == 5 and b["stopPrice"] is None and b["limitPrice"] is None
    assert b["side"] == t.stop_plan["side"] and b["size"] == t.filled_size
    assert b["trailPrice"] == 5010.0 - d and (b["trailPrice"] / 0.25) % 1 == 0 and b["trailPrice"] > 1000   # a level, never the raw distance
    assert t.trail is True and t.stop_state == "ACKED" and t.stop_current == b["trailPrice"] and not g.halted()


async def test_stale_quote_falls_back_to_the_planned_stop_level(tmp_path):
    g, t, gw, stops = await run_entry(tmp_path, fresh_quote=False, trail_entry=True)
    b = stops[0]
    assert b["type"] == 5 and b["stopPrice"] is None and b["trailPrice"] == t.stop_plan["stop_price"]
    assert t.trail is True and not g.halted()


async def test_missing_quote_still_a_trail(tmp_path):
    g, live, clock, _ = build2(tmp_path, trail_entry=True)
    gw = TB()
    g.client, g.verify_interval = gw, 0
    t = await staged(g)
    await go(g, t)
    live.quotes.pop(CID, None)
    await g.handle_position(fill(live))
    b = next(b for p, b in gw.writes if p == ORDER_PLACE and b.get("type") == 5)
    assert b["trailPrice"] == t.stop_plan["stop_price"]


async def test_resize_of_a_trailed_entry_stop_halts_instead_of_modifying(tmp_path):
    g, t, gw, stops = await run_entry(tmp_path, trail_entry=True)
    gw.writes.clear()
    await g._resize_stop(t, 2)
    assert g.halted() and not any(p.endswith("modify") for p, _ in gw.writes)


async def test_unreadable_trail_in_the_book_flattens_and_halts(tmp_path):
    g, live, clock, _ = build2(tmp_path, trail_entry=True)
    gw = TB()
    gw.store = {"type": 4}                      # the broker's record is not a trail
    g.client, g.verify_interval = gw, 0
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    assert g.halted()


async def test_trail_wider_than_the_planned_stop_flattens_and_halts(tmp_path):
    g, live, clock, _ = build2(tmp_path, trail_entry=True)
    gw = TB()
    gw.store = {"stopPrice": 4900.0}            # the broker's trigger is far wider than the 4994 the size was computed for
    g.client, g.verify_interval = gw, 0
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    assert g.halted() and t.stop_state == "FAILED"


async def test_oversized_trail_record_fails_closed(tmp_path):
    g, live, clock, _ = build2(tmp_path, trail_entry=True)
    gw = TB()
    gw.store = {"size": 5}
    g.client, g.verify_interval = gw, 0
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    assert g.halted()


async def test_trail_size_mismatch_flattens_not_just_halts(tmp_path):
    g, t, gw, stops = await run_entry(tmp_path, trail_entry=True)
    await g._resize_stop(t, 2)
    assert g.halted() and t.abandoned


# ---- 2026-11-19: slow lookup, pre-warm, audit of how and how late the stop went out ----------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402


def audit_rows(g):
    return [json.loads(x) for x in g.limits.audit_path.read_text().splitlines() if x.strip()]


async def test_slow_lookup_falls_back_fixed_no_halt(tmp_path):
    g, live, clock, _ = build2(tmp_path, trail_entry=True)
    gw = TB()
    g.client, g.verify_interval = gw, 0
    g.sleep = asyncio.sleep                      # a real watchdog (5 s): the 2 s lookup bound must beat it
    t = await staged(g)
    await go(g, t)

    async def hung(cid):
        await asyncio.sleep(60)

    g._lookup = hung
    g._contracts.clear()
    await g.handle_position(fill(live))
    stops = [b for p, b in gw.writes if p == ORDER_PLACE and b.get("type") in (4, 5)]
    assert len(stops) == 1 and stops[0]["type"] == 4 and stops[0]["stopPrice"] == t.stop_plan["stop_price"]
    assert t.stop_state == "ACKED" and not g.halted()
    assert any(r.get("state") == "TRAIL_FALLBACK_FIXED" for r in audit_rows(g))


async def test_contract_prewarmed_at_preview(tmp_path):
    g, live, clock, _ = build2(tmp_path, trail_entry=True)
    calls = []
    orig = g._lookup

    async def counting(cid):
        calls.append(cid)
        return await orig(cid)

    g._lookup = counting
    g._contracts.clear()
    t = await staged(g)
    await asyncio.gather(*list(g._bg))
    assert calls and CID in g._contracts            # warm before place()
    clock.t += 31                                   # the cached record aged out between preview and place
    n = len(calls)
    await go(g, t)
    await asyncio.gather(*list(g._bg))
    assert len(calls) > n and g.clock() - g._contracts[CID][0] < 30
    # an aged record is served at once for the tick (stale_ok) while a refresh runs
    clock.t += 31

    async def hung(cid):
        await asyncio.sleep(60)

    g._lookup = hung
    rec = await asyncio.wait_for(g._contract(CID, stale_ok=True), 0.5)
    assert rec["tickSize"] == 0.25
    for tk in list(g._bg):
        tk.cancel()


async def test_stop_sending_row_has_via_lag_lookup(tmp_path):
    g, t, gw, stops = await run_entry(tmp_path, trail_entry=True)
    row = next(r for r in audit_rows(g) if r.get("event") == "stop" and r.get("state") == "SENDING")
    assert row["via"] == "hub" and isinstance(row["lookup_ms"], int) and row["lookup_ms"] >= 0
    assert isinstance(row["ms"], int) and row["ts"]          # ts kept for the tests and tools that parse it
    assert "lag_ms" not in row                               # no fill event was seen on this path


async def test_fill_without_position_event_arms_stop_via_probe(tmp_path):
    g, live, clock, gw = build2(tmp_path, trail_entry=True)
    tb = TB()
    g.client = tb
    t = await staged(g)
    await go(g, t)
    tb.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]       # REST knows the position; the hub never sent the event
    live._on_trade_fill({"id": 77, "accountId": 7, "contractId": CID, "orderId": t.broker_order_id, "size": 1, "price": 5000.25})
    await asyncio.gather(*list(g._bg))
    stops = [b for p, b in tb.writes if p == ORDER_PLACE and b.get("type") in (4, 5)]
    assert len(stops) == 1 and t.stop_state == "ACKED" and not g.halted()
    row = next(r for r in audit_rows(g) if r.get("state") == "SENDING")
    assert row["via"] == "probe" and isinstance(row["lag_ms"], int)
    live._emit("fill", {"orderId": t.broker_order_id, "contractId": CID})    # one probe per ticket: a second fill does nothing
    await asyncio.gather(*list(g._bg))
    assert len([1 for p, b in tb.writes if p == ORDER_PLACE and b.get("type") in (4, 5)]) == 1


async def test_probe_never_writes_entry(tmp_path):
    g, live, clock, gw = build2(tmp_path, trail_entry=True)
    tb = TB()
    g.client = tb
    t = await staged(g)
    await go(g, t)
    tb.writes.clear()
    reads = []
    orig = tb.open_positions

    async def counting(a):
        reads.append(1)
        return await orig(a)

    tb.open_positions = counting
    # no position anywhere: the probe reads once and writes nothing
    live._on_trade_fill({"id": 78, "accountId": 7, "contractId": CID, "orderId": t.broker_order_id})
    await asyncio.gather(*list(g._bg))
    assert len(reads) == 1 and tb.writes == [] and t.stop_state is None
    # a position event that arrives first means no read at all
    g2, live2, clock2, _ = build2(tmp_path / "b", trail_entry=True)
    tb2 = TB()
    g2.client = tb2
    t2 = await staged(g2)
    await go(g2, t2)
    await g2.handle_position(fill(live2))
    n = len(tb2.writes)
    live2._on_trade_fill({"id": 79, "accountId": 7, "contractId": CID, "orderId": t2.broker_order_id})
    await asyncio.gather(*list(g2._bg))
    assert len(tb2.writes) == n
    # an unknown order id or another contract never starts a probe
    live._on_trade_fill({"id": 80, "accountId": 7, "contractId": CID, "orderId": 123456})
    assert not g._bg
