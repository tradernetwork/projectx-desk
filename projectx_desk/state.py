"""Live desk state: quotes, ticks, account, positions, orders, trades.

Fed by the market and user hubs (px_hub.py), read by the web app and the MCP tools. Everything
here is read-only with respect to the broker. Fan-out to SSE clients is refcounted per contract.
"""
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from projectx_desk.tradeday import parse_ts, trade_date

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RESYNC = object()  # ticks may have been lost; the page must re-seed from REST
DOWN = object()    # hub dropped; the page should show "reconnecting"


def ts_ms(ts):
    """ISO string (any tz, 7-digit .NET fractions) or number -> int ms."""
    if isinstance(ts, (int, float)):
        return int(ts)
    return (parse_ts(ts) - EPOCH) // timedelta(milliseconds=1)


from projectx_desk.flow import FlowStore
from projectx_desk.ticks import TickBars


class LiveState:
    def __init__(self, market_hub=None, user_hub=None, flow_path=None, ticks_path=None, flow_legacy=None):
        self.market, self.user = market_hub, user_hub
        self.quotes = {}      # contractId -> last GatewayQuote payload (+ "_rx" monotonic time)
        self.account = {}     # last GatewayUserAccount for the selected account
        self.positions = {}   # position id -> payload (size 0 removes)
        self.orders = {}      # order id -> payload (terminal statuses removed)
        self.trades = []      # GatewayUserTrade payloads, newest last, capped
        self.account_id = None
        self.trades_seeded = False   # day P&L is unknown (None) until trades are read from REST
        self.positions_seeded = False
        self._touched = None         # ids changed by live events while a REST snapshot is in flight
        self._listeners = []         # callables(kind, payload) for position/order events (the guard's stop manager)
        self.ticks = TickBars(path=ticks_path)   # tick bars (N prints per bar) from the same tape; sizes from PX_TICK_BARS
        self.flow = FlowStore(path=flow_path, legacy_json=flow_legacy)   # real buy/sell volume per minute, built from every GatewayTrade we receive
        self._tape = []       # contracts whose trade tape stays subscribed with no browser open (oldest first)
        self._subs = {}       # contractId -> set[Queue]
        self._pinned = set()  # contracts whose quotes stay subscribed so the guard/MCP work with no browser open
        if market_hub:
            market_hub.on("GatewayTrade", self._on_trade)
            market_hub.on("GatewayQuote", self._on_quote)
            market_hub.listen(self._on_market_event)
        if user_hub:
            user_hub.on("GatewayUserAccount", self._on_account)
            user_hub.on("GatewayUserPosition", self._on_position)
            user_hub.on("GatewayUserOrder", self._on_order)
            user_hub.on("GatewayUserTrade", self._on_trade_fill)

    # ---- market side ----------------------------------------------------------------------

    MAX_TAPE = 4

    def tape_contracts(self):
        """Contracts whose trade tape is being recorded (a copy)."""
        return list(self._tape)

    async def _ensure_tape(self, cid):
        """Record the trade tape for `cid` (buy/sell volume, tick bars) whether or not a browser is open."""
        if cid in self._tape or self.market is None:
            return
        self._tape.append(cid)                    # reserve the slot BEFORE any await: two concurrent askers must not both add it
        old = self._tape.pop(0) if len(self._tape) > self.MAX_TAPE else None
        if old is not None:
            self.flow.mark_gap(old, int(time.time() * 1000))     # stopping the tape makes the time until it returns a hole
            self.ticks.mark_gap(old)
            if old not in self._subs:
                await self.market.unsubscribe("SubscribeContractTrades", old)
        await self.market.subscribe("SubscribeContractTrades", cid, unsub_target="UnsubscribeContractTrades")

    async def subscribe_ticks(self, cid, maxsize=5000):
        q = asyncio.Queue(maxsize=maxsize)
        first = cid not in self._subs
        self._subs.setdefault(cid, set()).add(q)
        if self.market:
            # The trade tape stays subscribed after the last browser leaves (up to MAX_TAPE contracts): buy/sell volume and
            # tick bars are built from it, and a closed or reloaded tab must not leave an unflagged hole in them.
            await self._ensure_tape(cid)
            if first:
                await self.market.subscribe("SubscribeContractQuotes", cid, unsub_target="UnsubscribeContractQuotes")
            self.market.start()
        return q

    MAX_PINNED = 4

    async def ensure_quote(self, cid, wait=2.0, max_age=5.0):
        """Keep `cid`'s quotes flowing (server-side) and wait briefly for a fresh one.

        Without this the market hub only runs while a browser tick stream is open, so an MCP client or an
        AI reading the digest with the page closed would see no quote and the guard would (rightly) refuse.
        """
        if self.market is None:
            return
        if cid not in self._pinned and len(self._pinned) < self.MAX_PINNED:
            self._pinned.add(cid)
            await self.market.subscribe("SubscribeContractQuotes", cid, unsub_target="UnsubscribeContractQuotes")
            self.market.start()
        await self._ensure_tape(cid)             # anything that asks for this contract (digest, MCP, guard) also starts the tape
        deadline = time.monotonic() + wait
        while True:
            q = self.quotes.get(cid)
            if q and time.monotonic() - q.get("_rx", 0) < max_age:
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.05)

    async def unsubscribe_ticks(self, cid, q):
        qs = self._subs.get(cid)
        if qs is None:
            return
        qs.discard(q)
        if not qs:
            del self._subs[cid]
            if self.market:
                if cid not in self._tape:        # the tape stays on for contracts we are recording (see subscribe_ticks)
                    await self.market.unsubscribe("SubscribeContractTrades", cid)
                if cid not in self._pinned:      # a pinned contract keeps its quotes for the guard and MCP
                    await self.market.unsubscribe("SubscribeContractQuotes", cid)
                    self.quotes.pop(cid, None)

    @staticmethod
    def _put(q, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:  # slow client: drop its backlog, force a re-seed
            while not q.empty():
                q.get_nowait()
            q.put_nowait(RESYNC)

    def _on_trade(self, cid, data):
        for t in (data if isinstance(data, list) else [data]):
            self.ticks.add_trade(cid, t, ts_ms)   # tick bars; never raises (a malformed trade marks a gap)
            try:
                self.flow.add(cid, ts_ms(t["timestamp"]), t.get("volume"), t.get("type"))
            except Exception:  # noqa: BLE001 - a malformed trade must never stop the tick stream
                self.flow.mark_gap(cid, int(time.time() * 1000))
            for q in list(self._subs.get(cid, ())):
                self._put(q, t)

    def _on_quote(self, cid, data):
        if isinstance(data, dict):
            # The gateway sends PARTIAL quote updates (only the fields that changed). Replacing the quote wiped bid/last
            # whenever an ask-only update arrived and blanked sizing for a beat, so merge. Unchanged fields stay valid only
            # while the stream is alive: _on_market_event / unsubscribe clear the quote, so nothing outlives an outage.
            fresh = {k: v for k, v in data.items() if v is not None}
            merged = {**self.quotes.get(cid, {}), **fresh}
            bid, ask = merged.get("bestBid"), merged.get("bestAsk")
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > ask:
                merged = dict(fresh)          # a crossed book means one side is stale: trust only what just arrived
            self.quotes[cid] = {**merged, "_rx": time.monotonic()}

    def _on_market_event(self, kind):
        if kind in ("resync", "down"):
            self.quotes.clear()               # after an outage the old bid/ask/last are unknowable: fail closed until fresh ones arrive
            self.flow.mark_gap(None, int(time.time() * 1000))
            self.ticks.mark_gap(None)
            item = RESYNC if kind == "resync" else DOWN
            for qs in self._subs.values():
                for q in qs:
                    self._put(q, item)

    # ---- user side ------------------------------------------------------------------------

    def select_account(self, account_id):
        self.account_id = account_id
        self.account, self.positions, self.orders, self.trades = {}, {}, {}, []
        self.trades_seeded = self.positions_seeded = False

    def _mine(self, d):
        # Fail closed: until an account is selected, accept nothing (never mix accounts).
        return isinstance(d, dict) and self.account_id is not None and d.get("accountId") == self.account_id

    def listen(self, fn):
        self._listeners.append(fn)

    def _emit(self, kind, d):
        for fn in self._listeners:
            try:
                fn(kind, d)
            except Exception as e:  # a bad listener must never break event handling
                print(f"live listener: {type(e).__name__}", flush=True)

    def _touch(self, kind, key):
        if self._touched is not None:
            self._touched.add((kind, key))

    def _on_account(self, d):
        if isinstance(d, dict) and self.account_id is not None and d.get("id") == self.account_id:
            self.account = d

    def _on_position(self, d):
        # A payload without a numeric size is malformed, not "closed": never delete on it.
        if self._mine(d) and "id" in d and isinstance(d.get("size"), (int, float)):
            self._touch("pos", d["id"])
            if d["size"] == 0:
                self.positions.pop(d["id"], None)
            else:
                self.positions[d["id"]] = d
            self._emit("position", d)

    # OrderStatus per docs/projectx: 1 Open, 2 Filled, 3 Cancelled, 4 Expired, 5 Rejected, 6 Pending.
    _OPEN_STATUSES = (1, 6)

    def _on_order(self, d):
        if self._mine(d) and "id" in d:
            self._touch("ord", d["id"])
            if d.get("status") in self._OPEN_STATUSES:
                self.orders[d["id"]] = d
            else:
                self.orders.pop(d.get("id"), None)

    def _on_trade_fill(self, d):
        if self._mine(d):
            if d.get("id") is not None and any(t.get("id") == d.get("id") for t in self.trades):
                return                      # a replayed fill must not count twice (wins, losses, streaks, P&L)
            self.trades.append(d)
            del self.trades[:-500]

    def begin_seed(self):
        """Start tracking ids that live events touch while a REST snapshot is being fetched."""
        self._touched = set()

    def seed(self, positions, orders):
        """Apply a REST snapshot. Entries a live event changed since begin_seed() win over it,
        so a snapshot taken before an event can never overwrite the newer state."""
        touched = self._touched or set()
        self._touched = None
        self.positions = {**{p["id"]: p for p in positions if ("pos", p["id"]) not in touched},
                          **{k: v for k, v in self.positions.items() if ("pos", k) in touched}}
        self.orders = {**{o["id"]: o for o in orders
                          if o.get("status") in self._OPEN_STATUSES and ("ord", o["id"]) not in touched},
                       **{k: v for k, v in self.orders.items() if ("ord", k) in touched}}
        self.positions_seeded = True

    def seed_trades(self, trades):
        """Replace today's fills from REST (boot and every resync). Live fills already seen are kept."""
        have = {t.get("id") for t in trades}
        live_only = [t for t in self.trades if t.get("id") not in have]
        self.trades = (list(trades) + live_only)[-500:]
        self.trades_seeded = True

    # ---- derived --------------------------------------------------------------------------

    def closed_trades(self, now=None):
        """Today's (CT session) closed trades as [(datetime, net pnl)], oldest first. Fills without realised P&L
        (entries) and scratches are skipped. None if trades are not seeded (unknown, never an empty list)."""
        if not self.trades_seeded:
            return None
        today = trade_date(now or datetime.now(timezone.utc))
        by_order, out = {}, []
        for t in self.trades:
            pnl = t.get("profitAndLoss")
            if t.get("voided") or not isinstance(pnl, (int, float)) or isinstance(pnl, bool) or pnl == 0:
                continue
            try:
                ts = parse_ts(t["creationTimestamp"])
            except (KeyError, ValueError, AttributeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if trade_date(ts) != today:
                continue
            net = pnl - (t.get("fees") or 0.0)
            oid = t.get("orderId")
            if oid is not None:                 # one order filled in pieces is ONE trade, not several losses
                prev = by_order.get(oid)
                by_order[oid] = (max(ts, prev[0]), prev[1] + net) if prev else (ts, net)
            else:
                out.append((ts, net))
        out.extend(by_order.values())
        out.sort(key=lambda x: x[0])
        return out

    def day_pnl(self, now=None):
        """Realised P&L less fees for the current CT trading day.

        None means UNKNOWN (trades not yet read from REST), never zero: a caller must refuse to
        open on None. Unrealised P&L is not included; the drawdown logic must add it separately.
        """
        if not self.trades_seeded:
            return None
        today = trade_date(now or datetime.now(timezone.utc))
        total = 0.0
        for t in self.trades:
            if t.get("voided"):
                continue
            try:
                ts = parse_ts(t["creationTimestamp"])
            except (KeyError, ValueError, AttributeError):
                continue
            if trade_date(ts) == today:
                total += (t.get("profitAndLoss") or 0.0) - (t.get("fees") or 0.0)
        return round(total, 2)


# ---- allowlisted views: the ONLY shape of broker data that may reach an AI-readable surface ----
# Numbers and enums only. Free text from the gateway (names, tags, descriptions) is dropped, so a
# hostile string cannot become a prompt. customTag is shown only if it is one of ours.

OWN_TAG = re.compile(r"^px-[0-9a-f]{8}-\d{1,4}$")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def position_view(p):
    return {"contract": p.get("contractId") if re.match(r"^CON\.[A-Za-z0-9._]{1,40}$", str(p.get("contractId", ""))) else None,
            "side": {1: "LONG", 2: "SHORT"}.get(p.get("type")), "size": _num(p.get("size")),
            "avg_price": _num(p.get("averagePrice"))}


def order_view(o):
    tag = o.get("customTag")
    return {"contract": o.get("contractId") if re.match(r"^CON\.[A-Za-z0-9._]{1,40}$", str(o.get("contractId", ""))) else None,
            "side": {0: "BUY", 1: "SELL"}.get(o.get("side")),
            "type": {1: "LIMIT", 2: "MARKET", 3: "STOPLIMIT", 4: "STOP", 5: "TRAIL", 6: "JOINBID", 7: "JOINASK"}.get(o.get("type")),
            "status": {1: "OPEN", 6: "PENDING"}.get(o.get("status")),
            "size": _num(o.get("size")), "limit": _num(o.get("limitPrice")), "stop": _num(o.get("stopPrice")),
            "tag": ("OWN" if isinstance(tag, str) and OWN_TAG.match(tag) else "EXTERNAL") if tag else None}
