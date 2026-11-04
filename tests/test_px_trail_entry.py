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
