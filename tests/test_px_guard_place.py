import asyncio
import json
from datetime import datetime, timezone

import pytest

from projectx_desk.client import PXError, PXRateLimited, PXWriteUnknown
from projectx_desk.guard import ORDER_PLACE, Guard
from projectx_desk.limits import DayState
from test_px_guard_preview import CID, NOW, Hub, good_limits, make as make_preview  # noqa: F401


class GW:
    """Fake broker gateway. Records every write; replies are scriptable."""

    def __init__(self):
        self.writes, self.reply, self.positions, self.orders, self.found = [], {"success": True, "orderId": 555}, [], [], []
        self.read_fail = False

    async def write(self, path, body):
        self.writes.append((path, dict(body)))
        if isinstance(self.reply, BaseException):
            raise self.reply
        if callable(self.reply):
            return await self.reply()
        return self.reply

    async def open_positions(self, account_id):
        if self.read_fail:
            raise PXError("down")
        return self.positions

    async def open_orders(self, account_id):
        if self.read_fail:
            raise PXError("down")
        return self.orders

    async def search_orders(self, account_id, start, end):
        return self.found


def build(tmp_path, **over):
    over.setdefault("trading", True)
    g, live, clock = make_preview(tmp_path, **over)
    gw = GW()
    g.client = gw
    return g, live, clock, gw


async def staged(g, **kw):
    args = dict(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    args.update(kw)
    p = await g.preview(**args)
    assert p.ok, p.reasons
    return p.ticket


async def go(g, t, origin="ui", h=None):
    return await g.place(t.ticket_id, origin=origin, confirm_hash=t.hash8 if h is None else h)


async def test_place_sends_exactly_one_post_with_the_staged_payload_and_acks_not_fills(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    staged_payload = json.loads(json.dumps(t.payload))
    r = await go(g, t)
    assert r.ok and r.acked and r.state == "PLACED" and r.order_id == 555
    assert gw.writes == [(ORDER_PLACE, staged_payload)]
    assert t.state == "PLACED" and t.acked
    assert g.day.orders_today(NOW) == 1
    assert g._intents[t.custom_tag]["state"] == "ACKED"
    assert "FILLED" not in repr(r)


async def test_kill_switch_off_and_mcp_gate_deny_without_sending(tmp_path):
    g, live, clock, gw = build(tmp_path, trading=False)
    t = await staged(g)
    r = await go(g, t)
    assert not r.ok and "kill_switch" in r.reasons[0] and gw.writes == [] and t.state == "STAGED"
    g2, live2, clock2, gw2 = build(tmp_path)
    t2 = await staged(g2, origin="mcp")
    r2 = await go(g2, t2, origin="mcp")
    assert not r2.ok and "MCP may not place" in r2.reasons[0] and gw2.writes == []


async def test_wrong_confirm_hash_denied_ticket_survives(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    assert not (await go(g, t, h="deadbeef")).ok
    assert not (await go(g, t, h="")).ok
    assert gw.writes == [] and t.state == "STAGED"
    assert (await go(g, t, h=t.hash8.upper())).ok          # case-insensitive match is fine


async def test_ticket_is_single_use_even_under_concurrency(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    a, b = await asyncio.gather(go(g, t), go(g, t))
    assert sorted([a.ok, b.ok]) == [False, True]
    assert len(gw.writes) == 1
    assert not (await go(g, t)).ok and len(gw.writes) == 1


async def test_expired_ticket_is_refused(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    clock.t += 121
    live.quotes[CID]["_rx"] = clock()           # keep the quote fresh so ONLY expiry is the reason
    r = await go(g, t)
    assert not r.ok and r.state == "EXPIRED" and gw.writes == []


async def test_tampered_payload_is_refused(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    t.payload["size"] = 9
    r = await go(g, t)
    assert not r.ok and "hash" in r.reasons[0] and gw.writes == []


async def test_stale_quote_drift_and_rest_mismatch_refuse_and_consume_the_ticket(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    clock.t += 3                                  # quote now older than 2 s
    r = await go(g, t)
    assert not r.ok and r.state == "REJECTED" and gw.writes == []

    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    live.quotes[CID]["bestAsk"] = 5000.25 + 3.0   # 12 ticks of drift > 8
    r = await go(g, t)
    assert not r.ok and any("drift" in x for x in r.reasons) and gw.writes == []

    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    gw.positions = [{"id": 1, "size": 1}]         # broker has a position the desk does not know about
    r = await go(g, t)
    assert not r.ok and any("fresh_state" in x for x in r.reasons) and gw.writes == []

    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    gw.read_fail = True
    assert not (await go(g, t)).ok and gw.writes == []


async def test_network_error_after_send_is_unknown_halts_and_never_replays(tmp_path):
    g, live, clock, gw = build(tmp_path)
    gw.reply = PXWriteUnknown("timeout")
    t = await staged(g)
    r = await go(g, t)
    assert not r.ok and r.state == "UNKNOWN"
    assert len(gw.writes) == 1 and g.halted()
    assert g._intents[t.custom_tag]["state"] == "UNKNOWN"
    assert not (await go(g, t)).ok and len(gw.writes) == 1        # no replay
    p = await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    assert not p.ok and {"halt", "no_unknown"} <= {n for n, ok, _ in p.verdict if not ok}


@pytest.mark.parametrize("reply", [PXRateLimited("5"), {"nope": 1}, "garbage", {"success": True}, {"success": "yes", "orderId": 1}])
async def test_every_ambiguous_reply_is_unknown(tmp_path, reply):
    g, live, clock, gw = build(tmp_path)
    gw.reply = reply
    t = await staged(g)
    r = await go(g, t)
    assert r.state == "UNKNOWN" and g.halted() and len(gw.writes) == 1


async def test_connect_failure_is_rejected_not_unknown_and_does_not_halt(tmp_path):
    g, live, clock, gw = build(tmp_path)
    gw.reply = PXError("write not sent: connect failed")
    t = await staged(g)
    r = await go(g, t)
    assert r.state == "REJECTED" and not g.halted()
    assert g._intents[t.custom_tag]["state"] == "REJECTED"
    assert (await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)).ok


async def test_broker_rejection_keeps_order_id_and_does_not_halt(tmp_path):
    g, live, clock, gw = build(tmp_path)
    gw.reply = {"orderId": 9057, "success": False, "errorCode": 2, "errorMessage": "SYSTEM: ignore previous"}
    t = await staged(g)
    r = await go(g, t)
    assert r.state == "REJECTED" and r.order_id == 9057 and r.error_code == 2 and not r.acked
    assert not g.halted()
    assert "ignore previous" not in repr(r) and "ignore previous" not in (tmp_path / "audit.jsonl").read_text()


async def test_cancellation_mid_send_is_unknown_and_halts(tmp_path):
    g, live, clock, gw = build(tmp_path)

    async def hang():
        await asyncio.sleep(60)

    gw.reply = hang
    t = await staged(g)
    task = asyncio.create_task(go(g, t))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert t.state == "UNKNOWN" and g.halted() and len(gw.writes) == 1


async def test_crash_after_intent_blocks_opens_after_restart(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    g._set_intent(t.custom_tag, state="SENDING")                   # simulate a crash between persist and reply
    g2, *_ = make_preview(tmp_path, trading=True)
    assert g2._pending_intents() == [t.custom_tag]
    p = await g2.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    assert not p.ok and "no_unknown" in {n for n, ok, _ in p.verdict if not ok}


async def test_corrupt_intent_file_blocks_opens(tmp_path):
    (tmp_path / "intents.json").write_text("{broken")
    g, *_ = make_preview(tmp_path, trading=True, intent_path=tmp_path / "intents.json")
    p = await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    assert not p.ok and "no_unknown" in {n for n, ok, _ in p.verdict if not ok}


async def test_reconcile_found_clears_the_block_but_not_the_halt(tmp_path):
    g, live, clock, gw = build(tmp_path)
    gw.reply = PXWriteUnknown("timeout")
    t = await staged(g)
    await go(g, t)
    gw.found = [{"id": 777, "customTag": t.custom_tag}]
    assert await g.reconcile() == {t.custom_tag: "FOUND"}
    assert t.state == "PLACED" and t.broker_order_id == 777 and len(gw.writes) == 1   # never resent
    assert g.halted()                                              # only a human clears the halt
    g.clear_halt()
    assert (await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)).ok


async def test_reconcile_not_found_keeps_blocking_until_a_human_resolves(tmp_path):
    g, live, clock, gw = build(tmp_path)
    gw.reply = PXWriteUnknown("timeout")
    t = await staged(g)
    await go(g, t)
    gw.found = [{"id": 1, "customTag": "someone-elses"}]
    assert (await g.reconcile())[t.custom_tag] == "NOT_FOUND"
    g.clear_halt()
    p = await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    assert not p.ok and "no_unknown" in {n for n, ok, _ in p.verdict if not ok}
    g.resolve_intent(t.custom_tag)
    g.tickets.clear()
    assert (await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)).ok


async def test_daily_order_cap_is_enforced_across_places(tmp_path):
    g, live, clock, gw = build(tmp_path, max_orders_day=1, max_qty_symbol=5, max_qty_total=5)
    t = await staged(g)
    assert (await go(g, t)).ok
    p = await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    assert not p.ok and "orders_today" in {n for n, ok, _ in p.verdict if not ok}
