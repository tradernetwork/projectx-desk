"""Round 31: an entry is judged against the LIVE book, not only against its own entry price.

A limit at or through the far side trades at once (it is a market order the stop was never checked for), an off-tick limit is
rejected by the broker, and a post-fill stop at or through the market is rejected or triggers on the fill. Each is refused at
preview, by MCP preview, at readiness, and again synchronously at the commit point if the market moved during the broker read.
"""
import asyncio
import json

from fastapi.testclient import TestClient
from fastmcp import Client

from projectx_desk.guard import ORDER_PLACE, POSITION_CLOSE
from test_px_guard_place import build, go, staged
from test_px_guard_preview import CID, failed, make, prev
from test_px_guard_protect import build2, fill

BID, ASK = 5000.0, 5000.25          # the fixture book


async def test_a_limit_buy_at_or_above_the_ask_is_refused_and_one_under_it_rests(tmp_path):
    g, *_ = make(tmp_path)
    for px in (ASK, ASK + 0.25, ASK + 1.0):
        assert "limit_rests" in failed(await prev(g, order_type="limit", entry=px)), px
    ok = await prev(g, order_type="limit", entry=BID)              # at the bid: it rests
    assert ok.ok and ok.ticket.payload["limitPrice"] == BID


async def test_a_limit_sell_at_or_below_the_bid_is_refused_and_one_over_it_rests(tmp_path):
    g, *_ = make(tmp_path)
    for px in (BID, BID - 0.25, BID - 1.0):
        assert "limit_rests" in failed(await prev(g, side="short", stop_price=5006.0, order_type="limit", entry=px)), px
    ok = await prev(g, side="short", stop_price=5006.0, order_type="limit", entry=ASK)
    assert ok.ok and ok.ticket.payload["side"] == 1


async def test_an_off_tick_limit_price_is_refused(tmp_path):
    g, *_ = make(tmp_path)
    assert "entry_aligned" in failed(await prev(g, order_type="limit", entry=4999.9))
    assert "entry_aligned" in failed(await prev(g, order_type="limit", entry=4999.1))
    assert (await prev(g, order_type="limit", entry=4999.0)).ok


async def test_a_stop_on_the_wrong_side_of_the_live_book_is_refused_for_market_and_limit(tmp_path):
    g, live, clock = make(tmp_path, min_stop_ticks=1)
    # market long: the stop is under the ask (the entry) but AT the bid, so the sell stop would trigger on the fill
    p = await prev(g, stop_price=BID)
    assert "stop_clear_of_market" in failed(p) and "stop_side" not in failed(p)
    # market short: the stop is over the bid (the entry) but AT the ask
    p = await prev(g, side="short", stop_price=ASK)
    assert "stop_clear_of_market" in failed(p) and "stop_side" not in failed(p)
    # limit long in a wide book: under the limit price (stop_side passes) but above the live bid
    live.quotes[CID].update(bestBid=4990.0, bestAsk=5000.25)
    p = await prev(g, order_type="limit", entry=4999.0, stop_price=4994.0)
    assert "stop_clear_of_market" in failed(p) and "stop_side" not in failed(p) and "limit_rests" not in failed(p)
    # limit short in a wide book: over the limit price but under the live ask
    live.quotes[CID].update(bestBid=5000.0, bestAsk=5010.0)
    p = await prev(g, side="short", order_type="limit", entry=5001.0, stop_price=5006.0)
    assert "stop_clear_of_market" in failed(p) and "stop_side" not in failed(p)
    # a stop clear of the book passes
    live.quotes[CID].update(bestBid=BID, bestAsk=ASK)
    assert (await prev(g, order_type="limit", entry=4999.0, stop_price=4994.0)).ok


async def test_a_one_sided_missing_or_crossed_book_refuses(tmp_path):
    for q in ({"bestBid": None}, {"bestAsk": None}, {"bestBid": 0.0}, {"bestAsk": float("nan")}, {"bestBid": 5001.0, "bestAsk": 5000.0},
              {"bestBid": "5000.0"}):
        g, live, clock = make(tmp_path)
        live.quotes[CID].update(q)
        p = await prev(g)
        assert not p.ok and "quote_book" in failed(p), q
        assert "quote_book" in failed(await prev(g, order_type="limit", entry=4999.0)), q
    g, live, clock = make(tmp_path)
    live.quotes[CID].pop("bestBid")
    assert "quote_book" in failed(await prev(g))


async def test_readiness_runs_the_same_book_checks_and_names_them(tmp_path):
    g, live, clock = make(tmp_path, min_stop_ticks=1)
    r = await g.readiness(contract_id=CID, side="long", stop_price=BID, qty=1)
    assert not r["ready"] and "stop_clear_of_market" in {b["check"] for b in r["blockers"]}
    live.quotes[CID]["bestBid"] = None
    r = await g.readiness(contract_id=CID, side="long", stop_price=4994.0, qty=1)
    assert not r["ready"] and "quote_book" in {b["check"] for b in r["blockers"]}
    assert not g.tickets and not g.limits.audit_path.exists()          # read-only: no ticket, no audit line


def _move_book_during_read(g, live, **book):
    """The fresh-state REST read inside place() is where the market moves: the commit gate must see it."""
    orig = g.client.open_orders

    async def moving(account_id):
        live.quotes[CID].update(book)
        return await orig(account_id)

    g.client.open_orders = moving


async def test_commit_gate_refuses_when_the_market_reaches_the_limit_during_the_broker_read(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g, order_type="limit", entry=4999.0)
    _move_book_during_read(g, live, bestBid=4998.75, bestAsk=4999.0)       # the ask came down to the limit price
    r = await go(g, t)
    assert not r.ok and gw.writes == [] and t.state == "REJECTED"
    assert any("reached the limit price" in x for x in r.reasons)


async def test_commit_gate_refuses_when_the_stop_is_no_longer_clear_of_the_market(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g, order_type="limit", entry=4999.0, stop_price=4994.0)
    _move_book_during_read(g, live, bestBid=4993.75, bestAsk=4999.25)      # the bid fell through the stop; the limit still rests
    r = await go(g, t)
    assert not r.ok and gw.writes == [] and any("no longer clear of the market" in x for x in r.reasons)


async def test_commit_gate_refuses_a_book_that_lost_a_side_during_the_broker_read(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    _move_book_during_read(g, live, bestBid=None)
    r = await go(g, t)
    assert not r.ok and gw.writes == [] and any("two-sided book" in x for x in r.reasons)


async def test_place_reruns_the_book_checks_when_the_market_crossed_the_limit_before_place(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g, order_type="limit", entry=4999.0)
    live.quotes[CID].update(bestBid=4998.0, bestAsk=4998.75)              # the limit is now through the ask
    r = await go(g, t)
    assert not r.ok and gw.writes == [] and any(x.startswith("limit_rests") for x in r.reasons)


async def test_a_resting_limit_still_places_exactly_once(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g, order_type="limit", entry=4999.0)
    r = await go(g, t)
    assert r.ok and [p for p, _ in gw.writes] == [ORDER_PLACE] and gw.writes[0][1]["limitPrice"] == 4999.0


async def test_a_rejected_stop_after_a_limit_fill_still_flattens_and_halts_once(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    gw.stop_reply = {"success": False, "orderId": 901, "errorCode": 2}
    t = await staged(g, order_type="limit", entry=4999.0)
    assert (await go(g, t)).ok
    fill(live)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    await g.handle_position(live.positions[1])
    await g.handle_position(live.positions[1])                  # a repeated event must not send a second stop or a second flatten
    assert t.stop_state == "FAILED" and g.halted() and gw.pos == []
    assert len([1 for p, b in gw.writes if p == ORDER_PLACE and b["type"] == 4]) == 1
    assert len([1 for p, _ in gw.writes if p == POSITION_CLOSE]) == 1
    audit = [json.loads(x) for x in g.limits.audit_path.read_text(encoding="utf-8").splitlines()]
    assert len([a for a in audit if a.get("event") == "flatten"]) == 1
    assert len([a for a in audit if a.get("event") == "halt" and a.get("code") == "naked_position"]) == 1


def test_mcp_preview_of_a_marketable_limit_is_refused(tmp_path):
    from test_px_mcp import make as make_mcp
    app, g, live = make_mcp(tmp_path, trading=True)
    with TestClient(app):
        app.state.live = g.live

        async def run(entry):
            async with Client(app.state.mcp) as c:
                return (await c.call_tool("preview_order", {"contract": CID, "side": "long", "stop_price": 4994.0, "risk_pct": 0.04,
                                                             "order_type": "limit", "entry": entry})).data

        out = asyncio.run(run(ASK))
        assert not out["ok"] and any(x.startswith("limit_rests") for x in out["reasons"]) and "readback" not in out
        out = asyncio.run(run(4999.9))
        assert not out["ok"] and any(x.startswith("entry_aligned") for x in out["reasons"])
    assert not [t for t in g.tickets.values() if t.state == "STAGED"]
