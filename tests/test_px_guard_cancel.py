"""Round 31: the human CANCEL for a resting limit entry (Guard.cancel_entry, POST /api/cancel).

It takes a ticket id, never an order id, so it cannot reach a protective stop or an order the desk does not own; it refuses any fill
(a partial fill is a position: FLATTEN), a live account and a broker row that does not match the ticket; it sends ONE cancel and never
resends; and it leaves t.state alone so a fill that wins the race is still protected.
"""
import asyncio
import dataclasses
import json

from fastapi.testclient import TestClient

from projectx_desk.client import PXError, PXRateLimited, PXWriteUnknown
from projectx_desk.guard import ORDER_CANCEL, ORDER_PLACE
from test_px_app_actions import make_app, post
from test_px_guard_place import build, go, staged
from test_px_guard_preview import CID
from test_px_guard_protect import build2, fill


def _row(t, **over):
    r = {"id": 555, "contractId": CID, "status": 1, "type": 1, "side": 0 if t.side == "long" else 1, "size": 1,
         "limitPrice": t.entry_limit, "customTag": t.custom_tag}
    r.update(over)
    return r


async def resting(tmp_path, b=build, **over):
    g, live, clock, gw = b(tmp_path, **over)
    gw.reply = {"success": True, "orderId": 555}
    t = await staged(g, order_type="limit", entry=4999.0)
    assert (await go(g, t)).ok and t.broker_order_id == 555
    live.orders[555] = _row(t)                       # the hub lists it working
    gw.writes.clear()
    gw.reply = {"success": True}
    return g, live, clock, gw, t


def audit(g):
    return [json.loads(x) for x in g.limits.audit_path.read_text(encoding="utf-8").splitlines()]


def cancels(gw):
    return [b for p, b in gw.writes if p == ORDER_CANCEL]


async def test_cancel_a_resting_limit_sends_one_cancel_is_audited_and_reads_cancelled(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    r = await g.cancel_entry(t.ticket_id)
    assert r.ok and r.code == "cancelled"
    assert gw.writes == [(ORDER_CANCEL, {"accountId": 7, "orderId": 555})]
    assert t.state == "PLACED" and t.user_cancelled and g.entry_status(t) == "cancelled"
    live.orders.pop(555)
    assert g.entry_status(t) == "cancelled"
    row = [a for a in audit(g) if a["event"] == "cancel"][-1]
    assert row["code"] == "user" and row["state"] == "OK" and row["ticket"] == t.hash8
    r2 = await g.cancel_entry(t.ticket_id)
    assert not r2.ok and r2.code == "already_cancelled" and len(gw.writes) == 1


async def test_it_never_reaches_a_protective_stop(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    other = dataclasses.replace(t, ticket_id="other", state="CLOSED", stop_order_id=555)   # some ticket's stop carries this id
    g.tickets["other"] = other
    assert (await g.cancel_entry(t.ticket_id)).code == "protective_stop"
    del g.tickets["other"]
    for row in (_row(t, type=4, side=1, stopPrice=4994.0), _row(t, type=5, side=1, stopPrice=4994.0)):
        live.orders[555] = row
        assert (await g.cancel_entry(t.ticket_id)).code == "protective_stop"
    assert gw.writes == []


async def test_any_fill_refuses_partial_fills_go_through_flatten(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    t.filled_size = 1
    assert (await g.cancel_entry(t.ticket_id)).code == "has_fill"
    t.filled_size = 0
    live.trades.append({"id": 9, "orderId": 555, "contractId": CID, "size": 1, "voided": False})
    assert (await g.cancel_entry(t.ticket_id)).code == "has_fill"
    live.trades.clear()
    live.orders[555] = _row(t, fillVolume=1)
    assert (await g.cancel_entry(t.ticket_id)).code == "has_fill"
    assert gw.writes == []


async def test_only_a_placed_limit_entry_whose_broker_row_matches_the_ticket(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    assert (await g.cancel_entry("nope")).code == "not_entry"
    assert (await g.cancel_entry(555)).code == "not_entry"             # an order id is never accepted
    for row, code in ((_row(t, contractId="CON.F.US.MNQ.Z26"), "order_mismatch"), (_row(t, side=1), "order_mismatch"),
                      (_row(t, type=2), "order_mismatch"), (_row(t, customTag="px-deadbeef-1"), "order_mismatch"),
                      (_row(t, status=2), "not_working")):
        live.orders[555] = row
        assert (await g.cancel_entry(t.ticket_id)).code == code, row
    live.orders.pop(555)
    assert (await g.cancel_entry(t.ticket_id)).code == "not_working"
    live.orders[555] = {k: v for k, v in _row(t).items() if k != "customTag"}   # a row without a tag is judged on the rest
    g2, live2, clock2, gw2 = build(tmp_path / "m")
    tm = await staged(g2)
    await go(g2, tm)
    live2.orders[555] = {"id": 555, "contractId": CID, "status": 1, "type": 2, "side": 0, "size": 1}
    gw2.writes.clear()
    assert (await g2.cancel_entry(tm.ticket_id)).code == "not_entry"   # a market entry is not a resting limit
    assert gw.writes == [] and gw2.writes == []
    assert (await g.cancel_entry(t.ticket_id)).ok


async def test_a_live_account_makes_no_write(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    g.limits = dataclasses.replace(g.limits, account_kind="live")
    assert (await g.cancel_entry(t.ticket_id)).code == "live_cannot_cancel"
    g.limits = dataclasses.replace(g.limits, account_kind="sim")
    live.account["simulated"] = False                                   # config says sim, the broker says otherwise
    assert (await g.cancel_entry(t.ticket_id)).code == "live_cannot_cancel"
    assert gw.writes == []


async def test_allowed_while_halted_and_with_trading_off(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    g.trip_halt("manual")
    g.limits = dataclasses.replace(g.limits, trading=False)
    r = await g.cancel_entry(t.ticket_id)
    assert r.ok and len(cancels(gw)) == 1 and g.halt_cause() == "manual"


async def test_a_fill_that_wins_the_race_is_still_protected(tmp_path):
    # the cancel is ACKed, but the order had already filled at the broker: the position event must still arm the stop
    g, live, clock, gw, t = await resting(tmp_path, b=build2)
    assert (await g.cancel_entry(t.ticket_id)).ok and t.state == "PLACED"
    live.orders.pop(555)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    await g.handle_position(fill(live))
    stops = [b for p, b in gw.writes if p == ORDER_PLACE and b["type"] == 4]
    assert len(stops) == 1 and t.stop_state == "ACKED" and g.entry_status(t) == "filled" and not g.halted()


async def test_a_refused_cancel_then_a_fill_is_protected_as_usual(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path, b=build2)

    async def refused(path, body):
        gw.writes.append((path, dict(body)))
        return {"success": False, "errorCode": 2}
    orig, gw.write = gw.write, refused
    r = await g.cancel_entry(t.ticket_id)
    assert not r.ok and r.code == "cancel_failed" and not t.user_cancelled and len(cancels(gw)) == 1
    gw.write = orig
    await g.handle_position(fill(live))
    assert t.stop_state == "ACKED" and not g.halted()


async def test_an_unclear_cancel_is_read_back_and_never_resent(tmp_path):
    for err in (PXWriteUnknown("timeout"), PXRateLimited(5), RuntimeError("odd"), "malformed"):
        g, live, clock, gw, t = await resting(tmp_path / type(err).__name__)
        gw.reply = None if err == "malformed" else err
        gw.orders = [{"id": 555, "contractId": CID}]                     # REST still lists it
        r = await g.cancel_entry(t.ticket_id)
        assert not r.ok and r.code == "still_working" and len(cancels(gw)) == 1 and not g.halted() and not t.user_cancelled

        g, live, clock, gw, t = await resting(tmp_path / ("gone" + type(err).__name__))
        gw.reply = None if err == "malformed" else err
        gw.orders = []                                                   # REST no longer lists it: cancelled
        r = await g.cancel_entry(t.ticket_id)
        assert r.ok and r.code == "cancelled" and len(cancels(gw)) == 1 and not g.halted()


async def test_an_unclear_cancel_that_cannot_be_read_back_halts(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    gw.reply = PXWriteUnknown("timeout")
    gw.read_fail = True
    r = await g.cancel_entry(t.ticket_id)
    assert not r.ok and r.code == "cancel_unknown" and len(cancels(gw)) == 1
    assert g.halted() and g.halt_cause() == "cancel_unknown"
    assert [a for a in audit(g) if a["event"] == "cancel"][-1]["state"] == "UNKNOWN"


async def test_a_cancel_that_was_never_sent_is_a_plain_failure(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    gw.reply = PXError("connect failed")
    r = await g.cancel_entry(t.ticket_id)
    assert not r.ok and r.code == "cancel_failed" and not g.halted()


async def test_the_cancel_waits_for_an_in_flight_place_and_takes_one_write(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path)
    a, b = await asyncio.gather(g.cancel_entry(t.ticket_id), g.cancel_entry(t.ticket_id))
    assert sorted([a.code, b.code]) == ["already_cancelled", "cancelled"] and len(cancels(gw)) == 1


async def test_the_ttl_shares_the_checks_and_skips_a_user_cancelled_entry(tmp_path):
    g, live, clock, gw, t = await resting(tmp_path, limit_ttl_s=300)
    assert (await g.cancel_entry(t.ticket_id)).ok
    clock.t += 999
    assert await g.expire_resting_entries() == 0 and len(cancels(gw)) == 1
    g, live, clock, gw, t = await resting(tmp_path / "s", limit_ttl_s=300)
    live.orders[555] = _row(t, type=4, side=1, stopPrice=4994.0)       # the TTL can never reach a stop either
    clock.t += 999
    assert await g.expire_resting_entries() == 0 and gw.writes == []


def test_route_cancels_by_ticket_id_and_replies_with_a_fixed_code(tmp_path):
    app, g, live, gw = make_app(tmp_path)
    with TestClient(app) as c:
        app.state.live = g.live
        tok = c.get("/api/csrf").json()["token"]
        p = post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_dollars": 50,
                                          "order_type": "limit", "entry": 4999.0}).json()
        assert p["ok"], p["reasons"]
        gw.reply = {"success": True, "orderId": 555}
        tid = p["ticket"]["ticket_id"]
        assert post(c, "/api/place", tok, {"ticket_id": tid, "confirm_hash": p["ticket"]["hash8"]}).json()["ok"]
        live.orders[555] = _row(g.tickets[tid])
        assert post(c, "/api/cancel", tok, {"ticket_id": 555}).status_code == 400
        assert post(c, "/api/cancel", tok, {}).status_code == 400
        assert post(c, "/api/cancel", tok, {"ticket_id": "x" * 65}).status_code == 400
        assert post(c, "/api/cancel", tok, {"ticket_id": "nope"}).json() == {"ok": False, "code": "not_entry", "entry_status": None}
        assert post(c, "/api/cancel", tok, {"ticket_id": tid}, origin="http://evil.com").status_code == 403
        assert post(c, "/api/cancel", tok, {"ticket_id": tid}, **{"x-px-csrf": "guess"}).status_code == 403
        assert gw.cancelled == []
        r = post(c, "/api/cancel", tok, {"ticket_id": tid}).json()
        assert r == {"ok": True, "code": "cancelled", "entry_status": "cancelled"} and gw.cancelled == [555]
        assert c.get("/api/cancel").status_code == 405


def test_record_only_has_no_cancel_route_and_mcp_has_no_cancel_tool(tmp_path, monkeypatch):
    from projectx_desk.app import create_app
    from projectx_desk.hub import SignalRHub
    from test_px_app import FakeClient
    from test_px_mcp import EXPECTED_TOOLS, make as make_mcp
    for k in ("PX_RH", "PX_RECORD_ONLY", "PX_TRADING", "PX_MCP_PLACE"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeClient()
    rec = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"), user_hub=None,
                     allowed_hosts={"testserver"}, connect=False, record_only=True)
    assert "/api/cancel" not in {r.path for r in rec.routes}
    assert not [n for n in EXPECTED_TOOLS if "cancel" in n]
    app, g, live = make_mcp(tmp_path)
    with TestClient(app):
        from fastmcp import Client

        async def names():
            async with Client(app.state.mcp) as c:
                return {t.name for t in await c.list_tools()}
        got = asyncio.run(names())
    assert not [n for n in got if "cancel" in n.lower()]
