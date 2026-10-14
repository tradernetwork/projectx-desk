import pytest
from fastapi.testclient import TestClient

from px_client import PXError, PXWriteUnknown
from px_guard import ORDER_MODIFY, ORDER_PLACE
from test_px_app_actions import post
from test_px_guard_place import build, go, staged
from test_px_guard_preview import CID
from test_px_guard_protect import Broker, build2, fill


async def open_long(tmp_path, **over):
    """A filled long with an ACKED stop at 4994.0 (entry about 5000.25 ask)."""
    g, live, clock, gw = build2(tmp_path, **over)
    t = await staged(g)
    await go(g, t)
    await g.handle_position(fill(live))
    assert t.stop_state == "ACKED" and t.stop_current == 4994.0
    live.quotes[CID].update(bestBid=5010.0, bestAsk=5010.25, lastPrice=5010.0, _rx=clock())
    return g, live, clock, gw, t


async def test_stop_moves_up_for_a_long_and_the_modify_carries_the_right_fields(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    r = await g.move_stop(CID, 5000.5)
    assert r.ok and r.stop_price == 5000.5 and t.stop_current == 5000.5
    mod = [b for p, b in gw.writes if p == ORDER_MODIFY][-1]
    assert mod == {"accountId": 7, "orderId": 900, "size": 1, "limitPrice": None, "stopPrice": 5000.5, "trailPrice": None}


async def test_a_stop_can_never_be_widened_or_left_in_place(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    before = len(gw.writes)
    for px in (4990.0, 4994.0):                                  # lower, and equal to the current stop
        r = await g.move_stop(CID, px)
        assert not r.ok and "toward price" in r.reason
    assert len(gw.writes) == before and t.stop_current == 4994.0


async def test_a_stop_cannot_be_placed_at_or_through_the_market(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    for px in (5010.0, 5012.0):                                  # at the bid, and through it (tick 0.25)
        r = await g.move_stop(CID, px)
        assert not r.ok and "through the market" in r.reason
    assert (await g.move_stop(CID, 5009.75)).ok                  # exactly one tick clear of the bid is allowed


async def test_off_tick_stale_quote_bad_input_and_no_position_are_refused(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    assert "not on a tick" in (await g.move_stop(CID, 5001.1)).reason
    assert not (await g.move_stop(CID, float("nan"))).ok and not (await g.move_stop(CID, -5)).ok
    clock.t += 10
    assert "stale" in (await g.move_stop(CID, 5002.0)).reason
    assert not (await g.move_stop("CON.F.US.MNQ.Z26", 5002.0)).ok


async def test_live_accounts_are_refused_and_nothing_is_sent(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    g.limits = type(g.limits)(**{**g.limits.__dict__, "account_kind": "live"})
    before = len(gw.writes)
    r = await g.move_stop(CID, 5002.0)
    assert not r.ok and "live account" in r.reason and len(gw.writes) == before


async def test_works_while_halted_and_with_trading_off_because_it_only_reduces_risk(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    g.limits = type(g.limits)(**{**g.limits.__dict__, "trading": False})
    g.trip_halt("test")
    assert (await g.move_stop(CID, 5002.0)).ok


async def test_short_stops_only_move_down(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g, side="short", stop_price=5007.0)
    await go(g, t)
    await g.handle_position(fill(live, type_=2))
    assert t.stop_state == "ACKED" and t.stop_current == 5007.0
    live.quotes[CID].update(bestBid=4990.0, bestAsk=4990.25, lastPrice=4990.0, _rx=g.clock())
    assert not (await g.move_stop(CID, 5008.0)).ok                       # wider
    assert (await g.move_stop(CID, 5000.0)).ok and t.stop_current == 5000.0
    assert not (await g.move_stop(CID, 4990.25)).ok                      # at the ask: would fill


async def test_unknown_modify_outcome_is_verified_by_reading_the_broker(tmp_path):
    g, live, clock, gw, t = await open_long(tmp_path)
    orig = gw.write

    async def flaky(path, body):
        if path == ORDER_MODIFY:
            gw.writes.append((path, dict(body)))
            raise PXWriteUnknown("timeout")
        return await orig(path, body)

    gw.write = flaky
    gw.working = [{"id": 900, "stopPrice": 5000.5}]                       # the modify actually applied
    r = await g.move_stop(CID, 5000.5)
    assert r.ok and t.stop_current == 5000.5 and not g.halted()
    gw.working = [{"id": 900, "stopPrice": 5000.5}]                       # now it did NOT apply: old stop still there
    r2 = await g.move_stop(CID, 5002.0)
    assert not r2.ok and "old stop is still working" in r2.reason and t.stop_current == 5000.5 and not g.halted()
    gw.working = []                                                       # no stop at all: flatten and halt
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    r3 = await g.move_stop(CID, 5004.0)
    assert not r3.ok and g.halted() and gw.pos == []


def test_manage_block_and_digest_lines_and_the_stop_route(tmp_path):
    from test_px_app_actions import make_app
    from test_px_sizing_display import RECS, bars_newest_first
    import asyncio
    app, g, live, gw = make_app(tmp_path, size_scalers=False)
    with TestClient(app) as c:
        app.state.live = g.live
        async def lookup(cid):
            return RECS.get(cid) or {"id": cid, "name": "MESZ6", "activeContract": True, "tickSize": 0.25, "tickValue": 1.25}
        g._lookup = lookup
        async def bars(body):
            return bars_newest_first(atr=2.0, base=5000.0, amp=3.0)
        app.state.client.bars = bars
        tok = c.get("/api/csrf").json()["token"]
        p = post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_pct": 0.04}).json()
        assert p["ok"], p["reasons"]
        post(c, "/api/place", tok, {"ticket_id": p["ticket"]["ticket_id"], "confirm_hash": p["ticket"]["hash8"]})
        pos = fill(live)
        asyncio.run(g.handle_position(pos))
        live.quotes[CID].update(bestBid=5010.0, bestAsk=5010.25, lastPrice=5010.0, _rx=g.clock())
        s = c.get("/ai/state", params={"contract": CID}).json()
        m = s["manage"]
        assert m["side"] == "LONG" and m["stop"] == 4994.0 and m["open_r"] > 1.0
        names = {x["name"]: x for x in m["suggestions"]}
        assert names["BREAKEVEN"]["price"] == 5000.5 and names["BREAKEVEN"]["valid"] and names["BREAKEVEN"]["recommended"]
        assert all(x["price"] > 4994.0 for x in m["suggestions"] if x["valid"])
        text = c.get("/ai/digest", params={"contract": CID}).text
        assert "MANAGE: LONG 1" in text and "MANAGE_SUGGEST: BREAKEVEN 5000.5 valid=YES recommended=YES" in text
        r = post(c, "/api/stop", tok, {"contract": CID, "stop_price": 5000.5}).json()
        assert r["ok"] and r["stop_price"] == 5000.5
        assert not post(c, "/api/stop", tok, {"contract": CID, "stop_price": 4990.0}).json()["ok"]
        assert post(c, "/api/stop", tok, {"contract": CID, "stop_price": "x"}).status_code == 400
        assert post(c, "/api/stop", tok, {"contract": "bad", "stop_price": 1}).status_code == 400


def test_trade_block_follows_the_working_stop_after_a_move(tmp_path):
    import asyncio
    from test_px_app_actions import make_app
    app, g, live, gw = make_app(tmp_path, size_scalers=False)
    with TestClient(app) as c:
        app.state.live = g.live
        tok = c.get("/api/csrf").json()["token"]
        p = post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_pct": 0.04}).json()
        post(c, "/api/place", tok, {"ticket_id": p["ticket"]["ticket_id"], "confirm_hash": p["ticket"]["hash8"]})
        asyncio.run(g.handle_position(fill(live)))
        live.quotes[CID].update(bestBid=5010.0, bestAsk=5010.25, lastPrice=5010.0, _rx=g.clock())
        assert c.get("/ai/state").json()["trade"]["stop_price"] == 4994.0
        assert post(c, "/api/stop", tok, {"contract": CID, "stop_price": 5000.5}).json()["ok"]
        assert c.get("/ai/state").json()["trade"]["stop_price"] == 5000.5
