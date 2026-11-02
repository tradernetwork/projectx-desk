"""Guard.arm_trail: swap a position's fixed stop for a TRAIL (type 5), read it back, then drop the fixed one. Tighten only, sim only, default OFF."""
import asyncio

import pytest

from projectx_desk.client import PXWriteUnknown
from projectx_desk.guard import ORDER_CANCEL, ORDER_MODIFY, ORDER_PLACE
from test_px_guard_place import go, staged
from test_px_guard_preview import CID
from test_px_guard_protect import Broker, build2, fill

STOP = {"id": 900, "contractId": CID, "type": 4, "side": 1, "size": 1, "stopPrice": 4994.0, "status": 1, "trailPrice": None}


class TB(Broker):
    """A broker that understands type 5. Knobs: what it stores, whether it admits to the order, which cancels silently fail."""

    def __init__(self):
        super().__init__()
        self.store = {}              # overrides for the stored trail record
        self.visible = True
        self.exc = None
        self.stuck = set()

    async def write(self, path, body):
        if path == ORDER_PLACE and body["type"] == 5:
            self.writes.append((path, dict(body)))
            if self.visible:
                rec = {"id": 970, "contractId": body["contractId"], "type": 5, "side": body["side"], "size": body["size"],
                       "stopPrice": body["stopPrice"], "trailPrice": body["trailPrice"], "trailDistance": None, "status": 1,
                       "customTag": body["customTag"]}
                rec.update(self.store)
                self.working.append(rec)
            if self.exc:
                raise self.exc
            return {"success": True, "orderId": 970}
        if path == ORDER_CANCEL and body["orderId"] in self.stuck:
            self.writes.append((path, dict(body)))
            return {"success": True}                                  # says yes, the order stays
        return await super().write(path, body)


async def open_long(tmp_path, on=True, **over):
    g, live, clock, _ = build2(tmp_path, **over)
    gw = TB()
    g.client, g.verify_interval = gw, 0
    t = await staged(g)
    await go(g, t)
    pos = fill(live)
    await g.handle_position(pos)
    assert t.stop_state == "ACKED" and t.stop_current == 4994.0
    gw.pos, gw.working = [dict(pos)], [dict(STOP)]
    gw.writes.clear()
    live.quotes[CID].update(bestBid=5010.0, bestAsk=5010.25, lastPrice=5010.0, _rx=clock())
    set_limits(g, trail_stop=on, mcp_trail=False)
    return g, live, clock, gw, t


def set_limits(g, **kw):
    g.limits = type(g.limits)(**{**g.limits.__dict__, **kw})


def later(clock, live):
    clock.t += 10                                                  # past the per-contract throttle, with the quote kept fresh
    live.quotes[CID]["_rx"] = clock()


def kinds(gw):
    return [(p.rsplit("/", 1)[-1], b.get("type"), b.get("orderId")) for p, b in gw.writes]


async def test_off_by_default_nothing_is_sent(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path, on=False)
    r = await g.arm_trail(CID, 5.0)
    assert not r.ok and "off" in r.reason and gw.writes == []


async def test_happy_path_places_the_trail_reads_it_back_then_drops_the_fixed_stop(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    r = await g.arm_trail(CID, 5.0)
    assert r.ok and r.trigger == 5005.0 and r.order_id == 970 and r.reason == ""
    place, cancel = gw.writes
    assert place[0] == ORDER_PLACE and cancel == (ORDER_CANCEL, {"accountId": 7, "orderId": 900})            # trail first, THEN the old stop
    b = place[1]
    assert (b["type"], b["side"], b["size"], b["stopPrice"], b["trailPrice"], b["limitPrice"]) == (5, 1, 1, 5005.0, 5.0, None)
    assert b["customTag"].startswith("px-") and b["customTag"].endswith("-5")
    assert [o["id"] for o in gw.working] == [970]
    assert (t.stop_order_id, t.stop_current, t.trail, t.stop_state) == (970, 5005.0, True, "ACKED") and not g.halted()
    assert not any(p == ORDER_MODIFY for p, _ in gw.writes)


async def test_a_trailed_trade_cannot_be_modified_by_move_stop(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    assert (await g.arm_trail(CID, 5.0)).ok
    r = await g.move_stop(CID, 5008.0)
    assert not r.ok and "trail_stop" in r.reason


async def test_a_trail_that_would_start_wider_or_equal_is_refused(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    for d in (16.0, 20.0, 50.0):                          # 5010 - 16 = 4994 (equal), then wider
        later(clock, live)
        r = await g.arm_trail(CID, d)
        assert not r.ok and "only tightens" in r.reason
    assert gw.writes == [] and [o["id"] for o in gw.working] == [900]


@pytest.mark.parametrize("store, why", [
    ({"stopPrice": 4990.0}, "not tighter"),                       # broker put the trigger BELOW the old stop
    ({"stopPrice": 5010.0}, "through the market"),                # at the bid: would fill now
    ({"stopPrice": None}, "no readable trigger"),
    ({"trailPrice": 2.0}, "distance"),                            # not what we asked for
    ({"trailPrice": None}, "distance"),                           # unreadable distance fails closed
    ({"type": 4}, "not the order we sent"),
    ({"side": 0}, "not the order we sent"),
    ({"size": 0}, "not the order we sent"),
    ({"status": 3}, "not working"),
])
async def test_a_trail_that_does_not_read_back_is_cancelled_and_the_old_stop_stays(tmp_path, store, why):
    g, live, clock, gw, t = await open_long(tmp_path)
    gw.store = store
    r = await g.arm_trail(CID, 5.0)
    assert not r.ok and why in r.reason and "old stop left in place" in r.reason
    assert [c for p, c in gw.writes if p == ORDER_CANCEL] == [{"accountId": 7, "orderId": 970}]       # only the NEW trail was cancelled
    assert [o["id"] for o in gw.working] == [900] and t.stop_order_id == 900 and not t.trail and not g.halted()


async def test_distance_may_be_read_from_trailDistance_in_ticks(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    gw.store = {"trailPrice": None, "trailDistance": 20}          # 20 ticks of 0.25 = 5.0 points, as the broker stored the hand-placed trail
    assert (await g.arm_trail(CID, 5.0)).ok


async def test_a_trail_that_will_not_read_back_and_will_not_cancel_halts(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    gw.store, gw.stuck = {"trailPrice": 2.0}, {970}
    r = await g.arm_trail(CID, 5.0)
    assert not r.ok and "halted" in r.reason and g.halted() and t.stop_order_id == 900


async def test_an_old_stop_that_will_not_cancel_halts_but_the_trail_is_armed(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    gw.stuck = {900}
    r = await g.arm_trail(CID, 5.0)
    assert r.ok and "would not cancel" in r.reason and g.halted()
    assert t.stop_order_id == 970 and t.trail


async def test_an_unclear_reply_is_looked_up_by_tag_never_resent(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    gw.exc = PXWriteUnknown("timeout")                            # it reached the broker, the reply was lost
    r = await g.arm_trail(CID, 5.0)
    assert r.ok and [p for p, b in gw.writes if p == ORDER_PLACE and b["type"] == 5] == [ORDER_PLACE]      # exactly ONE place
    g2, live2, clock2, gw2, t2 = await open_long(tmp_path / "b")
    gw2.exc, gw2.visible = PXWriteUnknown("timeout"), False       # lost, and it never arrived
    r2 = await g2.arm_trail(CID, 5.0)
    assert not r2.ok and g2.halted() and [o["id"] for o in gw2.working] == [900]
    assert len([1 for p, b in gw2.writes if p == ORDER_PLACE]) == 1


async def test_refusals_send_nothing(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    for d, why in ((float("nan"), "invalid"), (-1, "invalid"), (5.1, "whole number"), (0.5, "ticks"), (500.0, "ticks")):
        later(clock, live)
        r = await g.arm_trail(CID, d)
        assert not r.ok and why in r.reason, (d, r.reason)
    later(clock, live)
    assert "needs exactly one open position" in (await g.arm_trail("CON.F.US.MNQ.Z26", 5.0)).reason
    later(clock, live)
    live.quotes[CID]["_rx"] = clock() - 10
    assert "stale" in (await g.arm_trail(CID, 5.0)).reason
    assert gw.writes == []


async def test_two_stops_or_an_existing_trail_or_a_small_stop_are_refused(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    trail = {**STOP, "id": 951, "type": 5, "stopPrice": 5000.0, "trailPrice": 5.0}
    gw.working = [dict(STOP), trail]
    assert "already trailing" in (await g.arm_trail(CID, 5.0)).reason
    later(clock, live)
    gw.working = [dict(STOP), {**STOP, "id": 952, "stopPrice": 4990.0}]
    assert "more than one stop" in (await g.arm_trail(CID, 5.0)).reason
    later(clock, live)
    gw.working = [{**STOP, "size": 0.5}]
    assert "unreadable or smaller" in (await g.arm_trail(CID, 5.0)).reason
    assert gw.writes == []


async def test_live_accounts_and_the_mcp_origin_are_gated(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    assert "may not arm" in (await g.arm_trail(CID, 5.0, origin="mcp")).reason
    set_limits(g, mcp_trail=True)
    later(clock, live)
    assert (await g.arm_trail(CID, 5.0, origin="mcp")).ok
    g2, live2, clock2, gw2, t2 = await open_long(tmp_path / "live")
    set_limits(g2, account_kind="live")
    assert "sim-only" in (await g2.arm_trail(CID, 5.0)).reason and gw2.writes == []


async def test_attempts_are_throttled_per_contract(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    gw.store = {"trailPrice": 2.0}
    assert not (await g.arm_trail(CID, 5.0)).ok
    n = len(gw.writes)
    assert "wait a few seconds" in (await g.arm_trail(CID, 5.0)).reason and len(gw.writes) == n


async def test_works_while_halted_and_with_trading_off_because_it_only_tightens(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    set_limits(g, trading=False)
    g.trip_halt("test")
    assert (await g.arm_trail(CID, 5.0)).ok


async def test_short_trail_mirrors(tmp_path):
    g, live, clock, _ = build2(tmp_path)
    gw = TB()
    g.client, g.verify_interval = gw, 0
    t = await staged(g, side="short", stop_price=5007.0)
    await go(g, t)
    pos = fill(live, type_=2)
    await g.handle_position(pos)
    gw.pos, gw.working = [dict(pos)], [{**STOP, "side": 0, "stopPrice": 5007.0}]
    gw.writes.clear()
    live.quotes[CID].update(bestBid=4990.0, bestAsk=4990.25, lastPrice=4990.0, _rx=clock())
    set_limits(g, trail_stop=True)
    assert not (await g.arm_trail(CID, 17.0)).ok                  # 4990.25 + 17 = 5007.25: wider
    later(clock, live)
    r = await g.arm_trail(CID, 5.0)
    assert r.ok and r.trigger == 4995.25
    assert gw.writes[0][1]["side"] == 0 and gw.writes[0][1]["stopPrice"] == 4995.25


def test_the_mcp_tool_exists_only_when_switched_on(tmp_path, monkeypatch):
    from fastmcp import Client
    from test_px_mcp import make

    def names():
        app, *_ = make(tmp_path)
        from fastapi.testclient import TestClient
        with TestClient(app):
            async def go_():
                async with Client(app.state.mcp) as c:
                    return {t.name for t in await c.list_tools()}
            return asyncio.run(go_())
    monkeypatch.delenv("PX_MCP_TRAIL", raising=False)
    assert "arm_trail" not in names()
    monkeypatch.setenv("PX_MCP_TRAIL", "1")
    assert "arm_trail" in names()
