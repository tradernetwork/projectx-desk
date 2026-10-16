"""Offline mock of the ProjectX Gateway, close enough to run the whole desk with no network.

    python dev_gateway.py                     # 127.0.0.1:8700 (DEV_GATEWAY_PORT overrides)
    PROJECTX_API=http://127.0.0.1:8700 PROJECTX_HUB_BASE=ws://127.0.0.1:8700/hubs \
    PROJECTX_USERNAME=dev PROJECTX_API_KEY=dev python px_app.py

It speaks the REST endpoints px_client.py uses (all POST, JSON, Bearer auth) and the SignalR
JSON-over-WebSocket protocol px_hub.py speaks (handshake {}, record separator 0x1e, pings type 6).
One SIM account (id 4242), four contracts (MES, MNQ, ES, NQ for the Z26 quarter), a price walk that
ticks about 4x per second, and an order book that fills market orders at the simulated bid/ask and
triggers resting stops/limits on a price cross. Fills are pushed over the user hub.

Control endpoints (loopback only, no bearer needed):
    GET  /dev/state                      everything the sim knows, for assertions
    POST /dev/fail?mode=M[&sticky=1][&off=1]
         M = place_timeout  next order placement is executed server-side, then the reply never arrives
             place_reject   next order placement is refused (errorCode 2); the order is still created
             drop_hub       close every hub socket now (kind=user|market narrows it); clients reconnect
             stale_quotes   stop publishing quotes and ticks until &off=1
    POST /dev/price?contract=CON...&px=5001.25   move the market and run the matching engine
    POST /dev/trade                      inject a historical trade (JSON body) for P&L bucketing tests

This is a simulator, not a model of the real gateway: the real one has sessions, margin, partial fills,
queue position and latency. It exists so the guard's failure paths can be driven on purpose.
"""
import asyncio
import base64
import hashlib
import json
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiohttp import WSMsgType, web

RS = "\x1e"
ACCOUNT_ID = 4242
QUARTER = "Z26"
UNIT_SECS = {1: 1, 2: 60, 3: 3600, 4: 86400, 5: 604800, 6: 2592000}
FEE_PER_CONTRACT = 0.37
_HANG = object()


# This file SERVES these endpoints (it is a mock gateway); it never calls them. test_px_guard_pin.py
# exempts it by name and separately asserts it imports no px_* client code.
P_PLACE, P_MODIFY, P_CANCEL = "/api/Order/place", "/api/Order/modify", "/api/Order/cancel"
P_CLOSE, P_PARTIAL = "/api/Position/closeContract", "/api/Position/partialCloseContract"
RAMP_BARS = 60  # recent history eases toward the live price over this many bars
BLOCK = 1024  # random-walk anchor spacing for bars: the walk restarts every BLOCK buckets

# root -> (symbolId, name stem, description, tickSize, tickValue, base price)
ROOTS = {
    "MES": ("F.US.MES", "Micro E-mini S&P 500", 0.25, 1.25, 5000.0),
    "MNQ": ("F.US.MNQ", "Micro E-mini Nasdaq-100", 0.25, 0.50, 17500.0),
    "ES": ("F.US.EP", "E-mini S&P 500", 0.25, 12.50, 5000.0),
    "NQ": ("F.US.ENQ", "E-mini Nasdaq-100", 0.25, 5.00, 17500.0),
}


def _contracts():
    out = {}
    for root, (sym, desc, tick, tv, base) in ROOTS.items():
        cid = f"CON.F.US.{root}.{QUARTER}"
        out[cid] = {"id": cid, "name": f"{root}{QUARTER[0]}{QUARTER[-1]}",
                    "description": f"{desc}: December 2026", "tickSize": tick, "tickValue": tv,
                    "activeContract": True, "symbolId": sym, "_base": base, "_root": root}
    return out


def _pub(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


def parse_ts(s):
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(s).replace("Z", "+00:00"))  # 7-digit .NET fractions
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _jwt():
    def b(x):
        return base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    head = b(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b(json.dumps({"sub": "dev", "exp": int(time.time()) + 86400, "jti": secrets.token_hex(8)}).encode())
    return f"{head}.{body}.{b(secrets.token_bytes(32))}"


class Conn:
    def __init__(self, kind, ws):
        self.kind, self.ws, self.subs, self.q = kind, ws, set(), asyncio.Queue()
        self.handshaken = False

    def send(self, target, *args):
        if self.handshaken:
            self.q.put_nowait({"type": 1, "target": target, "arguments": list(args)})


class DevGateway:
    def __init__(self, seed=7, walk=True, tick_s=0.25, ping_s=5.0, bracket_mode="position", now=None,
                 hold_s=30.0):
        self.seed, self.walk, self.tick_s, self.ping_s = seed, walk, tick_s, ping_s
        self.bracket_mode, self.hold_s = bracket_mode, hold_s
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.rng = random.Random(seed)
        self.contracts = _contracts()
        self.px = {cid: c["_base"] for cid, c in self.contracts.items()}
        self.hi, self.lo = dict(self.px), dict(self.px)
        self.open_px = dict(self.px)
        self.vol = {cid: 0 for cid in self.contracts}
        self.balance = 50000.0
        self.positions, self.orders, self.trades = {}, {}, []
        self.next_order, self.next_trade, self.next_pos = 1000, 5000, 100
        self.tokens, self.conns = set(), []
        self.fail = {}           # mode -> "once" | "sticky"
        self.stale = False
        self.hits = {}           # path -> request count
        self.write_log = []      # (path, customTag or None) for every order write
        self._walk_cache = {}
        self._wake = asyncio.Event()
        self._task = None
        self._runner = None
        self.app = self._build_app()

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self, host="127.0.0.1", port=0):
        self._runner = web.AppRunner(self.app, shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self._task = asyncio.create_task(self._loop(), name="dev-gateway-loop")
        return site._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._wake.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for c in list(self.conns):
            await c.ws.close()
        if self._runner:
            await self._runner.cleanup()

    # ---- market model ----------------------------------------------------------------------

    def tick(self, cid):
        return self.contracts[cid]["tickSize"]

    def bid(self, cid):
        return self.px[cid]

    def ask(self, cid):
        return round(self.px[cid] + self.tick(cid), 6)

    def _snap(self, cid, p):
        t = self.tick(cid)
        return round(round(p / t) * t, 6)

    def set_price(self, cid, p):
        p = self._snap(cid, p)
        self.px[cid] = p
        self.hi[cid], self.lo[cid] = max(self.hi[cid], p), min(self.lo[cid], p)
        self.vol[cid] += 1
        self._match(cid)

    def quote(self, cid):
        ts = self.now().isoformat()
        last = self.px[cid]
        c = self.contracts[cid]
        return {"symbol": c["symbolId"], "symbolName": f"/{c['_root']}", "lastPrice": last, "bestBid": self.bid(cid),
                "bestAsk": self.ask(cid), "change": round(last - self.open_px[cid], 6),
                "changePercent": round((last - self.open_px[cid]) / self.open_px[cid] * 100, 4),
                "open": self.open_px[cid], "high": self.hi[cid], "low": self.lo[cid], "volume": self.vol[cid],
                "lastUpdated": ts, "timestamp": ts}

    async def _loop(self):
        n, pings = 0, 0.0
        while True:
            await asyncio.sleep(self.tick_s)
            n += 1
            for cid in self.contracts:
                if self.walk:
                    step = self.rng.choice((-1, -1, 0, 1, 1)) * self.tick(cid)
                    drift = (self.contracts[cid]["_base"] - self.px[cid]) * 0.002  # keeps the walk near its base
                    self.set_price(cid, self.px[cid] + step + drift)
                if not self.stale:
                    for c in self.conns:
                        if c.kind == "market" and ("Quotes", cid) in c.subs:
                            c.send("GatewayQuote", cid, self.quote(cid))
                        if c.kind == "market" and ("Trades", cid) in c.subs:
                            c.send("GatewayTrade", cid, [{"symbolId": self.contracts[cid]["symbolId"],
                                                          "price": self.px[cid], "timestamp": self.now().isoformat(),
                                                          "type": self.rng.choice((0, 1)), "volume": 1}])
            pings += self.tick_s
            if pings >= self.ping_s:
                pings = 0.0
                for c in self.conns:
                    if c.handshaken:
                        c.q.put_nowait({"type": 6})

    # ---- order engine ----------------------------------------------------------------------

    def _event(self, name, payload, sub):
        for c in self.conns:
            if c.kind == "user" and sub in c.subs:
                c.send(name, payload)

    def _emit_order(self, o):
        self._event("GatewayUserOrder", _pub(o), ("Orders", o["accountId"]))

    def _emit_account(self):
        for c in self.conns:
            if c.kind == "user" and ("Accounts", None) in c.subs:
                c.send("GatewayUserAccount", self.account())

    def account(self):
        return {"id": ACCOUNT_ID, "name": "DEV-SIM", "balance": round(self.balance, 2), "canTrade": True,
                "isVisible": True, "simulated": True}

    def _new_order(self, b, status):
        ts = self.now().isoformat()
        self.next_order += 1
        o = {"id": self.next_order, "accountId": ACCOUNT_ID, "contractId": b["contractId"],
             "symbolId": self.contracts[b["contractId"]]["symbolId"], "creationTimestamp": ts, "updateTimestamp": ts,
             "status": status, "type": b["type"], "side": b["side"], "size": b["size"],
             "limitPrice": b.get("limitPrice"), "stopPrice": b.get("stopPrice"), "fillVolume": 0,
             "filledPrice": None, "customTag": b.get("customTag")}
        self.orders[o["id"]] = o
        return o

    def _set_status(self, o, status):
        o["status"], o["updateTimestamp"] = status, self.now().isoformat()
        self._emit_order(o)

    def _trade(self, cid, side, size, price, pnl, order_id):
        self.next_trade += 1
        fees = round(FEE_PER_CONTRACT * size, 4)
        t = {"id": self.next_trade, "accountId": ACCOUNT_ID, "contractId": cid,
             "creationTimestamp": self.now().isoformat(), "price": price, "profitAndLoss": pnl, "fees": fees,
             "side": side, "size": size, "voided": False, "orderId": order_id}
        self.trades.append(t)
        self.balance += (pnl or 0.0) - fees
        self._event("GatewayUserTrade", dict(t), ("Trades", ACCOUNT_ID))
        self._emit_account()

    def _apply(self, cid, side, size, price, order_id):
        """Net a fill into the contract's single position, emitting trades and position events."""
        c = self.contracts[cid]
        pos, remaining = self.positions.get(cid), size
        sgn = 1 if side == 0 else -1
        if pos and (1 if pos["type"] == 1 else -1) != sgn:
            close = min(pos["size"], remaining)
            direction = 1 if pos["type"] == 1 else -1
            pnl = round((price - pos["averagePrice"]) * direction * close / c["tickSize"] * c["tickValue"], 2)
            self._trade(cid, side, close, price, pnl, order_id)
            pos["size"] -= close
            remaining -= close
            self._event("GatewayUserPosition", _pub(pos), ("Positions", ACCOUNT_ID))
            if pos["size"] == 0:
                del self.positions[cid]
                pos = None
                for o in list(self.orders.values()):  # a flat contract carries no resting protection
                    if o["contractId"] == cid and o["status"] == 1:
                        self._set_status(o, 3)
        if remaining > 0:
            self._trade(cid, side, remaining, price, None, order_id)
            if pos:
                total = pos["size"] + remaining
                pos["averagePrice"] = round((pos["averagePrice"] * pos["size"] + price * remaining) / total, 6)
                pos["size"] = total
            else:
                self.next_pos += 1
                pos = {"id": self.next_pos, "accountId": ACCOUNT_ID, "contractId": cid,
                       "creationTimestamp": self.now().isoformat(), "type": 1 if sgn > 0 else 2, "size": remaining,
                       "averagePrice": price}
                self.positions[cid] = pos
            self._event("GatewayUserPosition", _pub(pos), ("Positions", ACCOUNT_ID))

    def _fill(self, o, price):
        o["fillVolume"], o["filledPrice"] = o["size"], price
        self._set_status(o, 2)
        self._apply(o["contractId"], o["side"], o["size"], price, o["id"])
        sl = o.get("_sl")
        if sl and o["contractId"] in self.positions:  # native OCO bracket: stop attached on fill
            t = self.tick(o["contractId"])
            sp = price - sl * t if o["side"] == 0 else price + sl * t
            stop = self._new_order({"contractId": o["contractId"], "type": 4, "side": 1 - o["side"],
                                    "size": o["size"], "stopPrice": self._snap(o["contractId"], sp)}, 1)
            self._emit_order(stop)

    def _match(self, cid):
        last = self.px[cid]
        for o in list(self.orders.values()):
            if o["contractId"] != cid or o["status"] != 1:
                continue
            if o["type"] == 4:
                sp = o["stopPrice"]
                if o["side"] == 0 and last >= sp:
                    self._fill(o, max(sp, self.ask(cid)))
                elif o["side"] == 1 and last <= sp:
                    self._fill(o, min(sp, self.bid(cid)))
            elif o["type"] == 1:
                lp = o["limitPrice"]
                if o["side"] == 0 and self.ask(cid) <= lp:
                    self._fill(o, lp)
                elif o["side"] == 1 and self.bid(cid) >= lp:
                    self._fill(o, lp)

    def _market_fill(self, o):
        cid = o["contractId"]
        self._fill(o, self.ask(cid) if o["side"] == 0 else self.bid(cid))

    # ---- REST ------------------------------------------------------------------------------

    def _build_app(self):
        app = web.Application()
        for path, fn in {
            "/api/Account/search": self.r_accounts, "/api/Contract/search": self.r_contract_search,
            "/api/Contract/searchById": self.r_contract_by_id, "/api/History/retrieveBars": self.r_bars,
            "/api/Position/searchOpen": self.r_pos_open, "/api/Order/searchOpen": self.r_ord_open,
            "/api/Order/search": self.r_ord_search, "/api/Trade/search": self.r_trade_search,
            P_PLACE: self.r_place, P_MODIFY: self.r_modify,
            P_CANCEL: self.r_cancel, P_CLOSE: self.r_close,
            P_PARTIAL: self.r_partial, "/api/Auth/validate": self.r_validate,
        }.items():
            app.router.add_post(path, self._rest(path, fn))
        app.router.add_post("/api/Auth/loginKey", self._login)
        app.router.add_get("/hubs/market", lambda r: self._hub(r, "market"))
        app.router.add_get("/hubs/user", lambda r: self._hub(r, "user"))
        app.router.add_get("/dev/state", self._dev_guard(self.d_state))
        app.router.add_post("/dev/fail", self._dev_guard(self.d_fail))
        app.router.add_post("/dev/price", self._dev_guard(self.d_price))
        app.router.add_post("/dev/trade", self._dev_guard(self.d_trade))
        return app

    def _authed(self, request):
        h = request.headers.get("Authorization", "")
        return h.startswith("Bearer ") and h[7:] in self.tokens

    def _rest(self, path, fn):
        async def handler(request):
            self.hits[path] = self.hits.get(path, 0) + 1
            if not self._authed(request):
                return web.Response(status=401, text="Unauthorized")
            try:
                b = await request.json()
            except ValueError:
                return web.json_response(self._err(2, "bad json"), status=400)
            if not isinstance(b, dict):
                return web.json_response(self._err(2, "object expected"), status=400)
            res = await fn(b)
            if res is _HANG:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.hold_s)
                except asyncio.TimeoutError:
                    pass
                if request.transport is not None:
                    request.transport.close()
                return web.Response(status=504)
            return web.json_response(res)
        return handler

    @staticmethod
    def _err(code, msg):
        return {"success": False, "errorCode": code, "errorMessage": msg}

    @staticmethod
    def _ok(**kw):
        return {**kw, "success": True, "errorCode": 0, "errorMessage": None}

    async def _login(self, request):
        self.hits["/api/Auth/loginKey"] = self.hits.get("/api/Auth/loginKey", 0) + 1
        try:
            b = await request.json()
        except ValueError:
            b = {}
        if not isinstance(b, dict) or not b.get("userName") or not b.get("apiKey"):
            return web.json_response({"token": None, "success": False, "errorCode": 3,
                                      "errorMessage": "Invalid credentials"})
        t = _jwt()
        self.tokens.add(t)
        return web.json_response({"token": t, "success": True, "errorCode": 0, "errorMessage": None})

    async def r_validate(self, b):
        return self._ok(newToken=None)

    async def r_accounts(self, b):
        return self._ok(accounts=[self.account()])

    async def r_contract_search(self, b):
        text = str(b.get("searchText", "")).upper()
        return self._ok(contracts=[_pub(c) for c in self.contracts.values()
                                   if text in c["name"].upper() or text in c["_root"]][:20])

    async def r_contract_by_id(self, b):
        c = self.contracts.get(b.get("contractId"))
        return self._ok(contract=_pub(c)) if c else self._err(8, "Contract not found")

    async def r_pos_open(self, b):
        if b.get("accountId") != ACCOUNT_ID:
            return self._err(1, "Account not found")
        return self._ok(positions=[_pub(p) for p in self.positions.values()])

    async def r_ord_open(self, b):
        if b.get("accountId") != ACCOUNT_ID:
            return self._err(1, "Account not found")
        return self._ok(orders=[_pub(o) for o in self.orders.values() if o["status"] in (1, 6)])

    def _window(self, b):
        try:
            start = parse_ts(b["startTimestamp"])
            end = parse_ts(b["endTimestamp"]) if b.get("endTimestamp") else None
        except (KeyError, ValueError, TypeError):
            return None
        return start, end

    async def r_ord_search(self, b):
        w = self._window(b)
        if b.get("accountId") != ACCOUNT_ID or w is None:
            return self._err(2, "bad request")
        return self._ok(orders=[_pub(o) for o in self.orders.values()
                                if parse_ts(o["creationTimestamp"]) >= w[0]
                                and (w[1] is None or parse_ts(o["creationTimestamp"]) <= w[1])])

    async def r_trade_search(self, b):
        w = self._window(b)
        if b.get("accountId") != ACCOUNT_ID or w is None:
            return self._err(2, "bad request")
        rows = [dict(t) for t in self.trades if parse_ts(t["creationTimestamp"]) >= w[0]
                and (w[1] is None or parse_ts(t["creationTimestamp"]) <= w[1])]
        return self._ok(trades=sorted(rows, key=lambda t: t["creationTimestamp"], reverse=True))

    # ---- bars ------------------------------------------------------------------------------

    def _level(self, cid, period, idx):
        blk, off = divmod(idx, BLOCK)
        key = (cid, period, blk)
        arr = self._walk_cache.get(key)
        if arr is None:
            r = random.Random(f"{self.seed}:{cid}:{period}:{blk}")
            t, x, arr = self.tick(cid), 0.0, []
            for _ in range(BLOCK):
                x += r.choice((-2, -1, -1, 0, 1, 1, 2)) * t
                arr.append(x)
            self._walk_cache[key] = arr
        return self._snap(cid, self.contracts[cid]["_base"] + arr[off])

    async def r_bars(self, b):
        cid = b.get("contractId")
        unit, n, limit = b.get("unit"), b.get("unitNumber"), b.get("limit")
        if cid not in self.contracts or unit not in UNIT_SECS or not isinstance(n, int) or n < 1 \
                or not isinstance(limit, int) or limit < 1:
            return self._err(2, "bad bars request")
        try:
            start, end = parse_ts(b["startTime"]), parse_ts(b["endTime"])
        except (KeyError, ValueError, TypeError):
            return self._err(2, "bad time range")
        period = UNIT_SECS[unit] * n
        limit = min(limit, 20000)
        idx = int(end.timestamp() // period)
        if not b.get("includePartialBar"):
            idx -= 1
        first = int(-(-start.timestamp() // period))
        t, rows = self.tick(cid), []
        # The synthetic history is an independent walk, so by itself it ends nowhere near the live price and the
        # join is a spike. Ease the most recent RAMP bars toward the live price so charts look continuous and the
        # indicators agree with the quote. Older bars are untouched.
        now_idx = int(self.now().timestamp() // period)
        delta = self.px[cid] - self._level(cid, period, now_idx)
        while idx >= first and len(rows) < limit:
            o, c = self._level(cid, period, idx - 1), self._level(cid, period, idx)
            h = hashlib.blake2b(f"{self.seed}:{cid}:{period}:{idx}".encode(), digest_size=4).digest()
            wick_h, wick_l, vol = (h[0] % 3) * t, (h[1] % 3) * t, 50 + h[2] * 4 + h[3]
            w = max(0.0, min(1.0, 1.0 - (now_idx - idx) / RAMP_BARS))
            sh = self._snap(cid, delta * w) if w else 0.0
            o2, c2 = o + sh, c + sh
            rows.append({"t": datetime.fromtimestamp(idx * period, timezone.utc).isoformat(),
                         "o": round(o2, 6), "h": round(max(o2, c2) + wick_h, 6), "l": round(min(o2, c2) - wick_l, 6),
                         "c": round(c2, 6), "v": vol})
            idx -= 1
        return self._ok(bars=rows)

    # ---- order writes ----------------------------------------------------------------------

    def _take(self, mode):
        kind = self.fail.get(mode)
        if kind == "once":
            del self.fail[mode]
        return kind is not None

    def _reject(self, b, msg, create=True):
        o = None
        if create and b.get("contractId") in self.contracts and b.get("type") in (1, 2, 4) and b.get("side") in (0, 1) \
                and isinstance(b.get("size"), int) and b["size"] > 0:
            o = self._new_order(b, 5)
            self._emit_order(o)
        return {"orderId": o["id"] if o else None, "success": False, "errorCode": 2, "errorMessage": msg}

    async def r_place(self, b):
        self.write_log.append((P_PLACE, b.get("customTag")))
        timeout = self._take("place_timeout")
        reject = self._take("place_reject")
        res = self._place(b, reject)
        return _HANG if timeout else res

    def _place(self, b, force_reject):
        if b.get("accountId") != ACCOUNT_ID:
            return {"orderId": None, "success": False, "errorCode": 1, "errorMessage": "Account not found"}
        cid = b.get("contractId")
        if cid not in self.contracts:
            return {"orderId": None, "success": False, "errorCode": 8, "errorMessage": "Contract not found"}
        if b.get("type") not in (1, 2, 4) or b.get("side") not in (0, 1) or not isinstance(b.get("size"), int) \
                or isinstance(b.get("size"), bool) or b["size"] < 1:
            return self._reject(b, "Unsupported order type, side or size")
        if force_reject:
            return self._reject(b, "dev: forced rejection")
        tag = b.get("customTag")
        if tag and any(o.get("customTag") == tag for o in self.orders.values()):
            return self._reject(b, "Duplicate customTag")
        if b["type"] == 1 and not isinstance(b.get("limitPrice"), (int, float)):
            return self._reject(b, "limitPrice required")
        if b["type"] == 4 and not isinstance(b.get("stopPrice"), (int, float)):
            return self._reject(b, "stopPrice required")
        brackets = b.get("stopLossBracket") or b.get("takeProfitBracket")
        if brackets and self.bracket_mode == "position":
            return self._reject(b, "Brackets cannot be used with Position Brackets. You must enable Auto OCO Brackets.")
        o = self._new_order(b, 1)
        if b.get("stopLossBracket"):
            o["_sl"] = int(b["stopLossBracket"]["ticks"])
        self._emit_order(o)
        if b["type"] == 2:
            self._market_fill(o)
        else:
            self._match(cid)
        return {"orderId": o["id"], "success": True, "errorCode": 0, "errorMessage": None}

    async def r_modify(self, b):
        self.write_log.append((P_MODIFY, None))
        o = self.orders.get(b.get("orderId"))
        if b.get("accountId") != ACCOUNT_ID or not o or o["status"] != 1:
            return self._err(2, "Order not open")
        if isinstance(b.get("size"), int) and b["size"] > 0:
            o["size"] = b["size"]
        if b.get("limitPrice") is not None:
            o["limitPrice"] = b["limitPrice"]
        if b.get("stopPrice") is not None:
            o["stopPrice"] = b["stopPrice"]
        o["updateTimestamp"] = self.now().isoformat()
        self._emit_order(o)
        self._match(o["contractId"])
        return self._ok()

    async def r_cancel(self, b):
        self.write_log.append((P_CANCEL, None))
        o = self.orders.get(b.get("orderId"))
        if b.get("accountId") != ACCOUNT_ID or not o or o["status"] != 1:
            return self._err(2, "Order not open")
        self._set_status(o, 3)
        return self._ok()

    def _close(self, cid, size=None):
        pos = self.positions.get(cid)
        if not pos:
            return self._err(2, "Position not found")
        n = pos["size"] if size is None else size
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= pos["size"]:
            return self._err(2, "Invalid size")
        side = 1 if pos["type"] == 1 else 0
        o = self._new_order({"contractId": cid, "type": 2, "side": side, "size": n}, 1)
        self._emit_order(o)
        self._market_fill(o)
        return self._ok()

    async def r_close(self, b):
        self.write_log.append((P_CLOSE, None))
        if b.get("accountId") != ACCOUNT_ID:
            return self._err(1, "Account not found")
        return self._close(b.get("contractId"))

    async def r_partial(self, b):
        self.write_log.append((P_PARTIAL, None))
        if b.get("accountId") != ACCOUNT_ID:
            return self._err(1, "Account not found")
        return self._close(b.get("contractId"), b.get("size"))

    # ---- hubs ------------------------------------------------------------------------------

    async def _hub(self, request, kind):
        self.hits[f"/hubs/{kind}"] = self.hits.get(f"/hubs/{kind}", 0) + 1
        if request.query.get("access_token") not in self.tokens:
            return web.Response(status=401, text="Unauthorized")
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        conn = Conn(kind, ws)
        self.conns.append(conn)

        async def writer():
            try:
                while True:
                    await ws.send_str(json.dumps(await conn.q.get()) + RS)
            except (ConnectionResetError, RuntimeError):
                pass

        wtask = asyncio.create_task(writer())
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                for rec in filter(None, m.data.split(RS)):
                    try:
                        f = json.loads(rec)
                    except ValueError:
                        continue
                    if "protocol" in f:
                        await ws.send_str("{}" + RS)
                        conn.handshaken = True
                    elif f.get("type") == 1:
                        self._invoke(conn, f.get("target", ""), f.get("arguments") or [])
        finally:
            wtask.cancel()
            await asyncio.gather(wtask, return_exceptions=True)
            if conn in self.conns:
                self.conns.remove(conn)
        return ws

    def _invoke(self, conn, target, args):
        unsub = target.startswith("Unsubscribe")
        name = target[len("Unsubscribe" if unsub else "Subscribe"):]
        if not (unsub or target.startswith("Subscribe")):
            return
        key = (name.replace("Contract", ""), args[0] if args else None)
        if unsub:
            conn.subs.discard(key)
            return
        conn.subs.add(key)
        if conn.kind == "user" and name == "Accounts":
            conn.send("GatewayUserAccount", self.account())

    # ---- /dev control ----------------------------------------------------------------------

    def _dev_guard(self, fn):
        async def handler(request):
            if request.remote not in ("127.0.0.1", "::1"):
                return web.json_response({"detail": "loopback only"}, status=403)
            return await fn(request)
        return handler

    async def d_state(self, request):
        return web.json_response({
            "prices": dict(self.px), "stale_quotes": self.stale, "fail": dict(self.fail),
            "positions": [_pub(p) for p in self.positions.values()],
            "orders": [_pub(o) for o in self.orders.values()],
            "open_orders": [_pub(o) for o in self.orders.values() if o["status"] in (1, 6)],
            "trades": [dict(t) for t in self.trades], "balance": round(self.balance, 2),
            "hits": dict(self.hits), "writes": [list(w) for w in self.write_log],
            "conns": {k: sum(1 for c in self.conns if c.kind == k) for k in ("user", "market")},
        })

    async def d_fail(self, request):
        mode = request.query.get("mode", "")
        off = request.query.get("off") == "1"
        sticky = request.query.get("sticky") == "1"
        if mode == "stale_quotes":
            self.stale = not off
        elif mode in ("place_timeout", "place_reject"):
            if off:
                self.fail.pop(mode, None)
            else:
                self.fail[mode] = "sticky" if sticky else "once"
        elif mode == "drop_hub":
            kind = request.query.get("kind")
            victims = [c for c in self.conns if kind in (None, c.kind)]
            for c in victims:
                await c.ws.close()
            return web.json_response({"dropped": len(victims)})
        else:
            return web.json_response({"detail": "unknown mode"}, status=400)
        return web.json_response({"ok": True, "mode": mode, "off": off})

    async def d_price(self, request):
        cid = request.query.get("contract", "")
        try:
            p = float(request.query["px"])
        except (KeyError, ValueError):
            return web.json_response({"detail": "bad px"}, status=400)
        if cid not in self.contracts:
            return web.json_response({"detail": "unknown contract"}, status=404)
        self.set_price(cid, p)
        return web.json_response({"price": self.px[cid]})

    async def d_trade(self, request):
        b = await request.json()
        self.next_trade += 1
        t = {"id": self.next_trade, "accountId": ACCOUNT_ID, "contractId": b["contractId"],
             "creationTimestamp": b["creationTimestamp"], "price": b.get("price", 0.0),
             "profitAndLoss": b.get("profitAndLoss"), "fees": b.get("fees", 0.0), "side": b.get("side", 1),
             "size": b.get("size", 1), "voided": bool(b.get("voided", False)), "orderId": b.get("orderId", 0)}
        self.trades.append(t)
        return web.json_response({"id": t["id"]})




async def _main():
    port = int(os.getenv("DEV_GATEWAY_PORT", "8700"))
    gw = DevGateway(walk=os.getenv("DEV_GATEWAY_WALK", "1") != "0")   # DEV_GATEWAY_WALK=0 holds the market still (use /dev/price)
    port = await gw.start("127.0.0.1", port)
    print(f"dev gateway on http://127.0.0.1:{port}  (SIM account {ACCOUNT_ID}, loopback only)", flush=True)
    print(f"  PROJECTX_API=http://127.0.0.1:{port} PROJECTX_HUB_BASE=ws://127.0.0.1:{port}/hubs", flush=True)
    print("  PROJECTX_USERNAME=dev PROJECTX_API_KEY=dev  (any non-empty pair logs in)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await gw.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
