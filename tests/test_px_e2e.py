"""End to end against dev_gateway.py: the real PXClient, SignalRHub, LiveState, Guard and px_app wiring,
talking REST and SignalR to the in-process mock gateway. Nothing in px_* is faked here; only the
broker is a simulator, so these prove the wiring and the failure paths, not the real gateway."""
import asyncio
import time
from contextlib import AsyncExitStack
from datetime import datetime, time as dtime, timedelta, timezone

import httpx
import pytest

import px_app
import px_guard
from dev_gateway import ACCOUNT_ID, DevGateway
from px_client import PXClient
from px_hub import SignalRHub
from px_limits import Limits
from px_time import CT, session_start

CID = "CON.F.US.MES.Z26"


def wall():
    """Real now, except inside the 15:45 to 17:00 CT no-new-entries window, where it is 15:00 CT today."""
    n = datetime.now(timezone.utc)
    ct = n.astimezone(CT)
    if dtime(15, 45) <= ct.time() < dtime(17, 0):
        return ct.replace(hour=15, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return n


async def until(pred, secs=5.0, what="condition"):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class Desk:
    pass


@pytest.fixture
async def make_desk(tmp_path, monkeypatch):
    stack = AsyncExitStack()

    async def start(preload=None, bracket_mode="position", **limit_over):
        monkeypatch.setenv("PX_ACCOUNT_ID", str(ACCOUNT_ID))
        monkeypatch.delenv("PX_MCP_TOKEN", raising=False)
        d = Desk()
        d.gw = DevGateway(walk=False, tick_s=0.1, ping_s=1.0, bracket_mode=bracket_mode, now=wall)
        if preload:
            preload(d.gw)
        port = await d.gw.start()
        stack.push_async_callback(d.gw.stop)
        d.url = f"http://127.0.0.1:{port}"
        d.ctl = httpx.AsyncClient(base_url=d.url)
        stack.push_async_callback(d.ctl.aclose)
        http = httpx.AsyncClient(timeout=0.6)
        stack.push_async_callback(http.aclose)
        d.client = PXClient(http=http, username="dev", api_key="dev", api=d.url)
        hubs = f"ws://127.0.0.1:{port}/hubs"
        d.market = SignalRHub(d.client, "market", url=f"{hubs}/market")
        d.user = SignalRHub(d.client, "user", url=f"{hubs}/user")
        d.kinds = []
        d.user.listen(d.kinds.append)
        limits = Limits(trading=True, account_id=ACCOUNT_ID, account_kind="sim", bracket_mode=bracket_mode,
                        daily_loss_limit=1000.0, dd_buffer=2000.0, no_new_after="15:45",
                        halt_path=tmp_path / "HALT", state_path=tmp_path / "day.json",
                        audit_path=tmp_path / "audit.jsonl", intent_path=tmp_path / "intents.json", **limit_over)

        def guard_factory(c, live, lim, user_hub=None):
            return px_guard.Guard(c, live, lim, user_hub=user_hub, wall=wall, verify_interval=0.05, verify_tries=40)

        monkeypatch.setattr(px_app, "Guard", guard_factory)
        d.app = px_app.create_app(client=d.client, market_hub=d.market, user_hub=d.user,
                                  allowed_hosts={"testserver"}, connect=True, limits=limits)
        await stack.enter_async_context(d.app.router.lifespan_context(d.app))
        d.api_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=d.app), base_url="http://testserver")
        stack.push_async_callback(d.api_http.aclose)
        d.live, d.guard = d.app.state.live, d.app.state.guard
        d.csrf = (await d.api_http.get("/api/csrf")).json()["token"]
        await until(lambda: d.user.up, what="user hub up")
        return d

    async def api(d, path, **body):
        r = await d.api_http.post(path, json=body, headers={"origin": "http://testserver", "x-px-csrf": d.csrf})
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    Desk.api = api
    yield start
    await stack.aclose()


async def quotes_flowing(d):
    await d.live.subscribe_ticks(CID)
    await until(lambda: CID in d.live.quotes, what="first quote")


async def open_long(d, **kw):
    args = dict(contract=CID, side="long", stop_price=4994.0, risk_dollars=50)
    args.update(kw)
    p = await d.api("/api/preview", **args)
    assert p["ok"], p["reasons"]
    r = await d.api("/api/place", ticket_id=p["ticket"]["ticket_id"], confirm_hash=p["ticket"]["hash8"])
    return p["ticket"], r


def entry_writes(d, tag):
    return [w for w in d.gw.write_log if w == ("/api/Order/place", tag)]


# ---- boot ------------------------------------------------------------------------------------

async def test_boot_seeds_account_positions_orders_and_todays_trades(make_desk):
    def preload(gw):
        gw._apply(CID, 0, 2, 5000.25, 1)   # an existing 2-lot long and its half-turn trade
    d = await make_desk(preload=preload)
    assert d.app.state.boot_error is None
    assert d.live.account_id == ACCOUNT_ID and d.live.account["simulated"] is True and d.live.account["canTrade"]
    assert d.live.positions_seeded and d.live.trades_seeded
    [pos] = d.live.positions.values()
    assert pos["size"] == 2 and pos["type"] == 1 and pos["contractId"] == CID
    assert len(d.live.trades) == 1 and d.live.day_pnl(wall()) == -0.74   # 2 x $0.37 fee, no realised P&L
    assert d.gw.hits["/api/Position/searchOpen"] >= 1 and d.gw.hits["/api/Trade/search"] >= 1
    await until(lambda: sum(1 for c in d.gw.conns if c.kind == "user") == 1, what="user hub connected")


async def test_empty_book_boot_reads_zero_not_unknown(make_desk):
    d = await make_desk()
    assert d.live.positions == {} and d.live.day_pnl() == 0.0
    st = (await d.api_http.get("/state.json")).json()
    assert st["boot_error"] is None and st["seeded"] == {"positions": True, "trades": True}


# ---- entry, fill, stop, flatten -----------------------------------------------------------------

async def test_preview_then_place_sends_one_post_fill_arrives_stop_is_placed_and_acked(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    assert d.gw.write_log == []                       # preview is read-only: nothing reached the broker
    p = await d.api("/api/preview", contract=CID, side="long", stop_price=4994.0, risk_dollars=50)
    assert p["ok"] and p["placeable"] and p["ticket"]["stop_plan"] == "separate_after_fill"
    assert d.gw.write_log == []
    tag = d.guard.tickets[p["ticket"]["ticket_id"]].custom_tag
    r = await d.api("/api/place", ticket_id=p["ticket"]["ticket_id"], confirm_hash=p["ticket"]["hash8"])
    assert r["ok"] and r["acked"] and r["state"] == "PLACED" and isinstance(r["order_id"], int)
    assert len(entry_writes(d, tag)) == 1

    await until(lambda: any(x["size"] == 1 for x in d.live.positions.values()), what="fill on the user hub")
    [pos] = d.live.positions.values()
    assert pos["type"] == 1 and pos["averagePrice"] == 5000.25      # market buy filled at the simulated ask

    t = d.guard.tickets[p["ticket"]["ticket_id"]]
    await until(lambda: t.stop_state == "ACKED", what="stop acked")
    stop_tag = f"px-{t.hash8}-2"
    assert [w for w in d.gw.write_log if w == ("/api/Order/place", stop_tag)] == [("/api/Order/place", stop_tag)]
    [stop] = [o for o in d.gw.orders.values() if o["customTag"] == stop_tag]
    assert stop["type"] == 4 and stop["side"] == 1 and stop["stopPrice"] == 4994.0 and stop["size"] == 1
    assert stop["status"] == 1
    await until(lambda: stop["id"] in d.live.orders, what="stop visible on the user hub")
    assert t.stop_order_id == stop["id"] and t.filled_size == 1


async def test_flatten_closes_the_position_and_cancels_the_stop(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    tk, _ = await open_long(d)
    t = d.guard.tickets[tk["ticket_id"]]
    await until(lambda: t.stop_state == "ACKED", what="stop acked")
    r = await d.api("/api/flatten")
    assert r["flat"] is True and r["orphan_orders"] == 0
    assert d.gw.positions == {}
    assert [o for o in d.gw.orders.values() if o["type"] == 4][0]["status"] == 3     # cancelled, not left behind
    await until(lambda: not d.live.positions and not d.live.orders, what="flat on the user hub")
    await until(lambda: t.state == "CLOSED", what="ticket closed")
    # bought the ask 5000.25, sold the bid 5000.00: one tick ($1.25) lost, plus $0.37 per fill
    await until(lambda: len(d.live.trades) == 2, what="both fills in the trade list")
    assert d.live.day_pnl(wall()) == -1.99
    assert not d.guard.halted()


async def test_a_resting_stop_triggers_on_a_price_cross_and_books_the_loss(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    tk, _ = await open_long(d)
    t = d.guard.tickets[tk["ticket_id"]]
    await until(lambda: t.stop_state == "ACKED", what="stop acked")
    await d.ctl.post("/dev/price", params={"contract": CID, "px": 4993.75})
    await until(lambda: not d.live.positions, what="stopped out")
    assert d.gw.positions == {} and not d.live.orders
    await until(lambda: len(d.live.trades) == 2, what="trades")
    # (4993.75 - 5000.25) = -6.5 pts = -26 ticks x $1.25 = -32.50, less 2 x $0.37
    assert d.live.day_pnl(wall()) == -33.24
    await until(lambda: t.state == "CLOSED", what="ticket closed")


# ---- forced failures ----------------------------------------------------------------------------

async def test_a_forced_reject_is_a_definite_refusal_with_no_position(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    assert (await d.ctl.post("/dev/fail", params={"mode": "place_reject"})).json()["ok"]
    _, r = await open_long(d)
    assert r["ok"] is False and r["state"] == "REJECTED" and r["error_code"] == 2
    assert d.gw.positions == {} and not d.guard.halted()
    assert [o["status"] for o in d.gw.orders.values()] == [5]       # created and rejected, as the docs say
    _, r2 = await open_long(d)                                       # one-shot: the next order goes through
    assert r2["ok"] and r2["acked"]


async def test_place_timeout_is_unknown_halts_sends_exactly_one_write_and_reconcile_finds_it(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    assert (await d.ctl.post("/dev/fail", params={"mode": "place_timeout"})).json()["ok"]
    p = await d.api("/api/preview", contract=CID, side="long", stop_price=4994.0, risk_dollars=50)
    assert p["ok"]
    t = d.guard.tickets[p["ticket"]["ticket_id"]]
    r = await d.api("/api/place", ticket_id=t.ticket_id, confirm_hash=t.hash8)
    assert r["ok"] is False and r["state"] == "UNKNOWN" and r["acked"] is False
    assert d.guard.halted()
    assert len(entry_writes(d, t.custom_tag)) == 1                   # never replayed
    assert len([o for o in d.gw.orders.values() if o["customTag"] == t.custom_tag]) == 1
    assert [p_["size"] for p_ in d.gw.positions.values()] == [1]     # it did reach the broker and fill

    # a new open is refused while an UNKNOWN order is outstanding (and the desk is halted)
    p2 = await d.api("/api/preview", contract=CID, side="long", stop_price=4994.0, risk_dollars=50)
    assert p2["ok"] is False

    rec = await d.api("/api/reconcile")
    assert rec == {t.custom_tag: "FOUND"} and t.state == "PLACED" and t.acked
    assert len(entry_writes(d, t.custom_tag)) == 1                   # reconcile reads, never resends
    assert d.guard.halted()                                          # only a human clears the halt
    assert (await d.api("/api/reconcile")) == {}                     # nothing left to reconcile
    assert (await d.api("/api/halt/clear"))["halted"] is False


async def test_unknown_position_is_still_protected_by_a_stop(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    await d.ctl.post("/dev/fail", params={"mode": "place_timeout"})
    p = await d.api("/api/preview", contract=CID, side="long", stop_price=4994.0, risk_dollars=50)
    t = d.guard.tickets[p["ticket"]["ticket_id"]]
    await d.api("/api/place", ticket_id=t.ticket_id, confirm_hash=t.hash8)
    await until(lambda: t.stop_state == "ACKED", what="stop on the unknown-state position")
    assert [o["type"] for o in d.gw.orders.values() if o["status"] == 1] == [4]


async def test_hub_drop_triggers_resync_and_reseed_without_losing_the_position(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    tk, _ = await open_long(d)
    t = d.guard.tickets[tk["ticket_id"]]
    await until(lambda: t.stop_state == "ACKED" and t.stop_order_id in d.live.orders, what="protected position")
    before = d.gw.hits["/api/Position/searchOpen"]
    assert (await d.ctl.post("/dev/fail", params={"mode": "drop_hub", "kind": "user"})).json()["dropped"] == 1
    await until(lambda: d.kinds[-2:] == ["down", "resync"], 8, "down then resync")
    await until(lambda: d.gw.hits["/api/Position/searchOpen"] > before and d.live.positions_seeded
                and d.live.trades_seeded, what="reseed from REST")
    assert [p_["size"] for p_ in d.live.positions.values()] == [1]
    assert t.stop_order_id in d.live.orders
    # subscriptions were replayed: a stop-out after the reconnect still reaches the desk
    await until(lambda: sum(1 for c in d.gw.conns if c.kind == "user" and ("Positions", ACCOUNT_ID) in c.subs) == 1,
                what="subscriptions replayed")
    await d.ctl.post("/dev/price", params={"contract": CID, "px": 4993.0})
    await until(lambda: not d.live.positions, what="stop-out seen after reconnect")


async def test_a_fill_missed_while_the_hub_was_down_is_recovered_by_the_reseed(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    tk, _ = await open_long(d)
    t = d.guard.tickets[tk["ticket_id"]]
    await until(lambda: t.stop_state == "ACKED", what="stop acked")
    await d.ctl.post("/dev/fail", params={"mode": "drop_hub", "kind": "user"})
    await d.ctl.post("/dev/price", params={"contract": CID, "px": 4993.0})   # stop fires while we are blind
    assert d.gw.positions == {}
    assert len(d.live.positions) == 1                                        # the desk has not heard yet
    await until(lambda: d.kinds[-1:] == ["resync"], 8, "resync")
    await until(lambda: not d.live.positions and not d.live.orders and len(d.live.trades) == 2, what="reseed")
    assert d.live.day_pnl(wall()) < 0


async def test_stale_quotes_make_preview_refuse_until_they_resume(make_desk):
    d = await make_desk()
    await quotes_flowing(d)
    await d.ctl.post("/dev/fail", params={"mode": "stale_quotes"})
    await asyncio.sleep(d.guard.limits.max_quote_age_s + 0.3)
    p = await d.api("/api/preview", contract=CID, side="long", stop_price=4994.0, risk_dollars=50)
    assert p["ok"] is False and any(v["check"] == "quote" and not v["ok"] for v in p["verdict"])
    await d.ctl.post("/dev/fail", params={"mode": "stale_quotes", "off": "1"})
    await until(lambda: time.monotonic() - d.live.quotes[CID]["_rx"] < 0.5, what="quotes resumed")
    assert (await d.api("/api/preview", contract=CID, side="long", stop_price=4994.0, risk_dollars=50))["ok"]
    assert d.gw.write_log == []


# ---- P&L bucketing -----------------------------------------------------------------------------

async def test_day_pnl_buckets_by_the_ct_session_and_ignores_voided_trades(make_desk):
    d = await make_desk()
    now = wall()
    s = session_start(now)

    async def inject(ts, pnl, fees=1.0, voided=False):
        r = await d.ctl.post("/dev/trade", json={"contractId": CID, "creationTimestamp": ts.isoformat(),
                                                 "profitAndLoss": pnl, "fees": fees, "voided": voided})
        return r.json()["id"]

    prev = await inject(s - timedelta(seconds=1), -500.0)            # 16:59:59 CT: the previous session
    mid = s + (now - s) / 2
    cur = await inject(mid, 40.0)                                     # this session
    voided = await inject(mid, 9999.0, voided=True)
    window = await d.client.trades(ACCOUNT_ID, session_start(now).isoformat(), (now + timedelta(minutes=1)).isoformat())
    assert {t["id"] for t in window} == {cur, voided}                 # REST honours the session start
    wide = await d.client.trades(ACCOUNT_ID, (s - timedelta(hours=2)).isoformat(), (now + timedelta(minutes=1)).isoformat())
    assert {t["id"] for t in wide} == {prev, cur, voided}
    d.live.seed_trades(wide)                                          # even handed the old trade, it must not count
    assert d.live.day_pnl(now) == 39.0                                # 40 - 1 fee; -500 is yesterday's, 9999 is voided
    assert d.live.day_pnl(now + timedelta(days=1)) == 0.0             # a later session starts clean
    assert d.live.day_pnl(s - timedelta(seconds=1)) == -501.0         # and the 16:59:59 trade belongs to the old one


# ---- the mock itself -----------------------------------------------------------------------------

async def test_gateway_rest_surface_auth_and_shapes(make_desk):
    d = await make_desk()
    raw = httpx.AsyncClient(base_url=d.url)
    try:
        for hdr in ({}, {"Authorization": "Bearer nope"}):
            r = await raw.post("/api/Account/search", json={"onlyActiveAccounts": True}, headers=hdr)
            assert r.status_code == 401
        bad = (await raw.post("/api/Auth/loginKey", json={"userName": "", "apiKey": "x"})).json()
        assert bad["success"] is False and bad["token"] is None
        tok = (await raw.post("/api/Auth/loginKey", json={"userName": "a", "apiKey": "b"})).json()["token"]
        assert tok.count(".") == 2
        h = {"Authorization": f"Bearer {tok}"}

        cs = (await raw.post("/api/Contract/search", json={"live": False, "searchText": "MES"}, headers=h)).json()
        assert [c["id"] for c in cs["contracts"]] == [CID]
        every = (await raw.post("/api/Contract/search", json={"live": False, "searchText": ""}, headers=h)).json()
        ticks = {c["id"].split(".")[3]: (c["tickSize"], c["tickValue"]) for c in every["contracts"]}
        assert ticks == {"MES": (0.25, 1.25), "MNQ": (0.25, 0.5), "ES": (0.25, 12.5), "NQ": (0.25, 5.0)}
        one = (await raw.post("/api/Contract/searchById", json={"contractId": CID}, headers=h)).json()
        assert one["contract"]["activeContract"] is True

        body = {"contractId": CID, "live": False, "startTime": "2026-10-01T00:00:00Z", "endTime": "2026-10-02T00:00:00Z",
                "unit": 2, "unitNumber": 5, "limit": 50, "includePartialBar": False}
        a = (await raw.post("/api/History/retrieveBars", json=body, headers=h)).json()["bars"]
        b = (await raw.post("/api/History/retrieveBars", json=body, headers=h)).json()["bars"]
        assert a == b and len(a) == 50                                 # deterministic, honours limit
        assert [x["t"] for x in a] == sorted((x["t"] for x in a), reverse=True)   # newest first
        assert all(x["l"] <= min(x["o"], x["c"]) and x["h"] >= max(x["o"], x["c"]) for x in a)

        accts = (await raw.post("/api/Account/search", json={"onlyActiveAccounts": True}, headers=h)).json()["accounts"]
        assert [(x["id"], x["canTrade"], x["simulated"]) for x in accts] == [(ACCOUNT_ID, True, True)]
        st = (await raw.get("/dev/state")).json()
        assert {"prices", "positions", "open_orders", "trades", "writes", "hits", "conns"} <= set(st)
    finally:
        await raw.aclose()


async def test_oco_bracket_mode_attaches_the_stop_on_fill_and_position_mode_rejects_brackets(make_desk):
    d = await make_desk(bracket_mode="oco")
    await quotes_flowing(d)
    tk, r = await open_long(d)
    assert r["ok"]
    t = d.guard.tickets[tk["ticket_id"]]
    assert t.stop_plan["mode"] == "oco"
    await until(lambda: [o for o in d.gw.orders.values() if o["type"] == 4 and o["status"] == 1], what="bracket stop")
    [stop] = [o for o in d.gw.orders.values() if o["type"] == 4]
    assert stop["stopPrice"] == 5000.25 - 25 * 0.25 and stop["side"] == 1

    gw = DevGateway(bracket_mode="position")
    res = gw._place({"accountId": ACCOUNT_ID, "contractId": CID, "type": 2, "side": 0, "size": 1,
                     "stopLossBracket": {"ticks": 10, "type": 4}}, False)
    assert res["success"] is False and res["errorCode"] == 2 and isinstance(res["orderId"], int)
    assert gw.positions == {}                                         # the order exists but is rejected, no fill
