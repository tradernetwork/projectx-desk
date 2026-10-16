import asyncio
import json

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from projectx_desk.hub import RS, SignalRHub


class FakeClient:
    def __init__(self):
        self.fresh = 0

    async def token(self, fresh=False):
        self.fresh += bool(fresh)
        return "jwt"


class Gateway:
    """Fake SignalR hub: answers the handshake, records invokes, can push events and drop."""

    def __init__(self):
        self.connections, self.invokes, self.sockets = 0, [], []

    async def ws(self, request):
        assert request.query["access_token"] == "jwt"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self.sockets.append(ws)
        async for m in ws:
            for rec in filter(None, m.data.split(RS)):
                f = json.loads(rec)
                if f.get("protocol"):
                    await ws.send_str("{}" + RS)
                elif f.get("type") == 1:
                    self.invokes.append((self.connections, f["target"], f["arguments"]))
        return ws

    async def push(self, target, *args):
        await self.sockets[-1].send_str(json.dumps({"type": 1, "target": target, "arguments": list(args)}) + RS)


async def until(pred, secs=3.0):
    for _ in range(int(secs / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def make_hub(gw):
    app = web.Application()
    app.router.add_get("/hub", gw.ws)
    srv = TestServer(app)
    await srv.start_server()
    hub = SignalRHub(FakeClient(), "market", url=f"ws://127.0.0.1:{srv.port}/hub", session=aiohttp.ClientSession())
    return srv, hub


async def test_handshake_subscribe_dispatch_and_replay_after_drop(monkeypatch):
    gw = Gateway()
    srv, hub = await make_hub(gw)
    events, kinds, got = [], [], []
    hub.listen(kinds.append)
    hub.on("GatewayTrade", lambda cid, d: got.append((cid, d)))
    try:
        await hub.subscribe("SubscribeContractTrades", "CON.X", unsub_target="UnsubscribeContractTrades")
        hub.start()
        await until(lambda: hub.up)
        assert kinds == ["up"]
        await until(lambda: (1, "SubscribeContractTrades", ["CON.X"]) in gw.invokes)

        await gw.push("GatewayTrade", "CON.X", {"price": 5})
        await until(lambda: got)
        assert got == [("CON.X", {"price": 5})]

        await gw.sockets[-1].close()                     # drop the connection
        await until(lambda: kinds[-2:] == ["down", "resync"], 6)
        await until(lambda: (2, "SubscribeContractTrades", ["CON.X"]) in gw.invokes)  # replayed
    finally:
        await hub.stop()
        await hub._session.close()
        await srv.close()


async def test_bad_handler_does_not_kill_stream():
    gw = Gateway()
    srv, hub = await make_hub(gw)
    got = []

    def boom(*a):
        raise ValueError("x")

    hub.on("GatewayQuote", boom)
    hub.on("GatewayTrade", lambda cid, d: got.append(d))
    try:
        hub.start()
        await until(lambda: hub.up)
        await gw.push("GatewayQuote", "C", {})
        await gw.push("GatewayTrade", "C", 1)
        await until(lambda: got == [1])
        assert hub.up
    finally:
        await hub.stop()
        await hub._session.close()
        await srv.close()


async def test_unsubscribe_sends_counterpart_and_forgets():
    gw = Gateway()
    srv, hub = await make_hub(gw)
    try:
        hub.start()
        await until(lambda: hub.up)
        await hub.subscribe("SubscribeContractTrades", "C", unsub_target="UnsubscribeContractTrades")
        await hub.unsubscribe("SubscribeContractTrades", "C")
        await until(lambda: any(t == "UnsubscribeContractTrades" for _, t, _ in gw.invokes))
        assert ("SubscribeContractTrades", ("C",)) not in hub._subs
    finally:
        await hub.stop()
        await hub._session.close()
        await srv.close()
