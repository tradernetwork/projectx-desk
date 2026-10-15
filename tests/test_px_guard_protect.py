import asyncio

import pytest

from px_client import PXError, PXWriteUnknown
from px_guard import ORDER_CANCEL, ORDER_MODIFY, ORDER_PLACE, POSITION_CLOSE, GuardError
from test_px_guard_place import CID, GW, build, go, staged  # noqa: F401


class Broker(GW):
    """Scripted broker: tracks a position, honours closeContract/market-close when told to."""

    def __init__(self):
        super().__init__()
        self.pos = []                # REST positions
        self.close_works = True
        self.market_close_works = True
        self.stop_reply = {"success": True, "orderId": 900}
        self.cancelled = []
        self.working = []            # REST open orders

    async def open_positions(self, account_id):
        return list(self.pos)

    async def open_orders(self, account_id):
        return list(self.working)

    async def write(self, path, body):
        self.writes.append((path, dict(body)))
        if path == POSITION_CLOSE:
            if self.close_works:
                self.pos = []
            return {"success": True}
        if path == ORDER_CANCEL:
            self.cancelled.append(body["orderId"])
            self.working = [o for o in self.working if o["id"] != body["orderId"]]
            return {"success": True}
        if path == ORDER_MODIFY:
            return {"success": True}
        if path == ORDER_PLACE:
            if body["type"] == 4:                      # a stop
                if isinstance(self.stop_reply, BaseException):
                    raise self.stop_reply
                return self.stop_reply
            if body["type"] == 2 and body["customTag"].endswith("-9"):    # flatten market order
                if self.market_close_works:
                    self.pos = []
                return {"success": True, "orderId": 1}
            r = self.reply
            if isinstance(r, BaseException):
                raise r
            if callable(r):
                return await r()
            return r
        raise AssertionError(path)


def build2(tmp_path, **over):
    g, live, clock, _ = build(tmp_path, **over)
    gw = Broker()
    g.client = gw
    g.verify_interval = 0

    async def nosleep(_):
        return None

    g.sleep = nosleep
    return g, live, clock, gw


def fill(live, size=1, type_=1, cid=CID):
    p = {"id": 1, "accountId": 7, "contractId": cid, "type": type_, "size": size, "averagePrice": 5000.25}
    live.positions[1] = p
    return p


async def test_fill_triggers_a_stop_sized_to_the_position_and_it_is_acked(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    stops = [b for p, b in gw.writes if p == ORDER_PLACE and b["type"] == 4]
    assert len(stops) == 1
    assert stops[0]["size"] == 1 and stops[0]["side"] == 1 and stops[0]["stopPrice"] == 4994.0
    assert stops[0]["customTag"] == f"px-{t.hash8}-2"
    assert t.stop_state == "ACKED" and t.stop_order_id == 900
    assert not g.halted()
    await asyncio.sleep(0)
    assert t.ticket_id not in g._watchdogs or g._watchdogs[t.ticket_id].cancelled() or g._watchdogs[t.ticket_id].done()


async def test_fill_arriving_while_place_is_in_flight_still_gets_a_stop(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    release = asyncio.Event()
    orig = gw.reply

    async def slow():
        await release.wait()
        return orig

    gw.reply = slow
    task = asyncio.create_task(go(g, t))
    await asyncio.sleep(0.02)
    assert t.state == "PLACING"
    stop_task = asyncio.create_task(g.handle_position(fill(live)))   # fill event beats the HTTP reply
    await asyncio.sleep(0.02)
    release.set()
    r = await task
    await stop_task
    assert r.ok and t.stop_state == "ACKED"


async def test_stop_rejected_flattens_and_halts(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.stop_reply = {"success": False, "orderId": 901, "errorCode": 2}
    t = await staged(g)
    await go(g, t)
    fill(live)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    await g.handle_position(live.positions[1])
    assert t.stop_state == "FAILED"
    assert any(p == POSITION_CLOSE for p, _ in gw.writes) and gw.pos == []
    assert g.halted()


async def test_stop_network_error_flattens_and_halts_with_no_retry(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.stop_reply = PXWriteUnknown("timeout")
    t = await staged(g)
    await go(g, t)
    fill(live)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    await g.handle_position(live.positions[1])
    stops = [1 for p, b in gw.writes if p == ORDER_PLACE and b["type"] == 4]
    assert len(stops) == 1 and g.halted() and gw.pos == []


async def test_watchdog_flattens_if_the_stop_never_acks(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]

    async def hang(*a, **k):
        await asyncio.sleep(60)

    gw.stop_reply = None
    orig_write = gw.write

    async def write(path, body):
        if path == ORDER_PLACE and body.get("type") == 4:
            gw.writes.append((path, dict(body)))
            await asyncio.sleep(60)
        return await orig_write(path, body)

    gw.write = write

    async def instant(_):
        return None

    g.sleep = instant                      # watchdog fires immediately
    p = fill(live)
    arm = asyncio.create_task(g.handle_position(p))
    await asyncio.sleep(0.1)
    assert g.halted() and gw.pos == []
    arm.cancel()
    await asyncio.gather(arm, return_exceptions=True)


async def test_position_closing_marks_the_ticket_closed(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    await g.handle_position({"id": 1, "contractId": CID, "type": 1, "size": 0})
    assert t.state == "CLOSED"


async def test_short_fill_ignored_for_a_long_ticket(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live, type_=2))
    assert t.stop_state is None


async def test_stop_resized_when_position_grows(tmp_path):
    g, live, clock, gw = build2(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3)
    t = await staged(g, risk_dollars=200.0)
    await go(g, t)
    await g.handle_position(fill(live, size=1))
    await g.handle_position(fill(live, size=3))
    mods = [b for p, b in gw.writes if p == ORDER_MODIFY]
    assert mods and mods[-1]["size"] == 3 and mods[-1]["orderId"] == 900 and t.stop_size == 3


async def test_flatten_works_with_trading_off_and_while_halted(tmp_path):
    g, live, clock, gw = build2(tmp_path, trading=False)
    g.trip_halt("test")
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 2}]
    r = await g.flatten()
    assert r.flat and gw.pos == [] and [p for p, _ in gw.writes] == [POSITION_CLOSE]


async def test_flatten_falls_back_to_an_opposite_market_order_capped_at_net_only_when_enabled(tmp_path):
    g, live, clock, gw = build2(tmp_path, flatten_fallback=True)
    gw.close_works = False
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 2}]
    r = await g.flatten(contract_id=CID)
    mk = [b for p, b in gw.writes if p == ORDER_PLACE]
    assert r.flat and len(mk) == 1 and mk[0]["side"] == 1 and mk[0]["size"] == 2 and mk[0]["type"] == 2
    assert not g.halted()


async def test_flatten_that_cannot_confirm_flat_halts(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.close_works = gw.market_close_works = False
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    r = await g.flatten()
    assert not r.flat and g.halted() and any("not confirmed clear" in x for x in r.reasons)


async def test_flatten_fallback_is_off_by_default_so_a_late_close_cannot_flip_the_position(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.close_works = False
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    r = await g.flatten(contract_id=CID)
    assert not r.flat and g.halted()
    assert [p for p, _ in gw.writes if p == ORDER_PLACE] == []        # no opposite order was ever sent
    assert any("close it in the TopstepX UI" in x for x in r.reasons)


async def test_flatten_on_sim_cancels_working_orders_first_live_reports_orphans(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    gw.working = [{"id": 900, "contractId": CID}]
    r = await g.flatten()
    assert r.flat and gw.cancelled == [900] and [p for p, _ in gw.writes][0] == ORDER_CANCEL

    g2, live2, clock2, gw2 = build2(tmp_path, account_kind="live", allow_live=True, bracket_mode="oco")
    live2.account["simulated"] = False
    gw2.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    gw2.working = [{"id": 901, "contractId": CID}]
    r2 = await g2.flatten()
    assert gw2.cancelled == [] and r2.orphan_orders == 1 and any("TopstepX UI" in x for x in r2.reasons)


async def test_cancel_is_sim_only_and_never_calls_the_api_on_live(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    assert await g.cancel(900)
    assert gw.cancelled == [900]
    g2, live2, clock2, gw2 = build2(tmp_path, account_kind="live", allow_live=True)
    with pytest.raises(GuardError):
        await g2.cancel(900)
    assert gw2.writes == []
    with pytest.raises(GuardError):
        await g.cancel("900; DROP")
