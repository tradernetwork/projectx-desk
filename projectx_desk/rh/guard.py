"""THE ROBINHOOD ORDER PATH: the second pinned writer (AGENTS.md, `tests/test_px_guard_pin.py`).

Mirrors `projectx_desk/guard.py`: preview (every check, a hashed single-use ticket), place (every check re-read at the
commit point), halt, audit. Equities only, whole shares, LIMIT orders, buy to open and sell to close, one working or
pending order per symbol. A write is sent only when Robinhood's own trade-approvals setting reads ON at the commit point,
so the final tap is the owner's, in the Robinhood app. The desk owns no stops: a fill is unprotected.

Rules with teeth: anything unreadable refuses; a write makes ONE attempt (`Client.submit`) and is never replayed; an
ambiguous outcome is UNKNOWN (persisted, halts RH, never resent); a reply that is not clearly `awaiting_approval` is treated
as live exposure and halts RH; only a human clears the halt (`clear_halt`, no MCP tool). Audit lines and halt words are
fixed vocabulary: no account number, no provider text. RH halt, audit and ticket files are separate from ProjectX's.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import views
from .limits import RhLimits
from .mcp import McpError, mint_write_cap
from .reads import Unknown

PLACE_TOOL = "place_equity_order"
_KEY = mint_write_cap(frozenset({PLACE_TOOL}))      # module-private: nothing else can submit a write

CODES = ("trading_off", "halted", "account_unknown", "approvals_off", "approvals_unknown", "bad_intent", "qty_cap",
         "notional_cap", "symbol_busy", "sell_exceeds_position", "no_position", "market_closed", "quote_stale", "cap_unset",
         # beyond the plan's list (reported in the task notes): reads that failed, and ticket lifecycle refusals
         "read_failed", "ticket_unknown", "ticket_used", "ticket_expired", "hash_mismatch", "state_unwritable")

PREVIEWED, SENDING, AWAITING, APPROVED, PLACED = "previewed", "sending", "awaiting_approval", "approved", "placed"
UNKNOWN, REJECTED, REFUSED, DISCARDED = "unknown", "rejected", "refused", "discarded"
DECLINED, EXPIRED, FILLED = "declined", "expired", "filled"
ACTIVE = {PREVIEWED, SENDING, AWAITING, APPROVED, PLACED, UNKNOWN}
WORKING = {"queued", "unconfirmed", "confirmed", "partially_filled", "pending", "unknown"}   # an unreadable status counts as working

SAFE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
AUDIT_KEYS = ("ts", "event", "ticket", "symbol", "side", "qty", "limit_price", "state", "code", "order_id")
WARNINGS = ("no_paper_trading", "fill_unprotected", "approve_in_app")
ET = ZoneInfo("America/New_York")


class Refused(Exception):
    """A preview or place the guard will not do. `.code` is one of CODES, `.detail` a short fixed phrase. Never provider text."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code if code in CODES else "bad_intent"
        self.detail = detail if isinstance(detail, str) and len(detail) < 80 else ""


def market_open_now(now=None) -> bool:
    """Regular US equity hours, Mon-Fri 09:30 to 16:00 ET. Holidays and half days are not modelled (a closed market refuses
    at the broker anyway; this only avoids sending an order into a closed session)."""
    local = (now or datetime.now(timezone.utc)).astimezone(ET)
    return local.weekday() < 5 and dtime(9, 30) <= local.time() < dtime(16, 0)


def _num(v):
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _fmt(x) -> str:
    return ("%.4f" % x).rstrip("0").rstrip(".")


@dataclass
class Ticket:
    ticket_id: str
    ref_id: str
    symbol: str
    side: str
    qty: int
    limit_price: float
    created: float
    expires_at: float
    hash8: str
    state: str = PREVIEWED
    order_id: Optional[str] = None
    approval_id: Optional[str] = None
    updated: float = 0.0

    @property
    def notional(self) -> float:
        return round(self.qty * self.limit_price, 2)

    def view(self) -> dict:
        """The allowlisted face of a ticket: validated enums and numbers only. No ref_id, order id, account or provider text."""
        return {"ticket_id": self.ticket_id, "hash8": self.hash8, "symbol": self.symbol, "side": self.side,
                "qty": self.qty, "limit_price": self.limit_price, "notional": self.notional,
                "expires_at": self.expires_at, "state": self.state, "warnings": list(WARNINGS)}


def _hash8(intent: dict, ref_id: str) -> str:
    blob = json.dumps({"symbol": intent["symbol"], "side": intent["side"], "qty": intent["qty"],
                       "limit_price": intent["limit_price"], "ref_id": ref_id}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


@dataclass
class _Eval:
    account: str = field(repr=False, default="")


class Guard:
    def __init__(self, reader, client, limits: RhLimits, audit_path, halt_path, state_path, clock: Callable = time.time,
                 market_open: Optional[Callable] = None):
        self.reader, self.client, self.limits = reader, client, limits
        self.audit_path, self.halt_path, self.state_path = Path(audit_path), Path(halt_path), Path(state_path)
        self.clock = clock
        self.market_open = market_open or (lambda: market_open_now(datetime.fromtimestamp(self.clock(), timezone.utc)))
        self.tickets: dict[str, Ticket] = {}
        self._lock = asyncio.Lock()

    # ---- halt and audit ---------------------------------------------------------------------------------------
    def halted(self) -> bool:
        try:
            return self.halt_path.exists()
        except OSError:
            return True                                   # an unreadable halt file is treated as halted

    def halt_cause(self) -> Optional[str]:
        if not self.halted():
            return None
        try:
            txt = self.halt_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return "unknown"
        return txt if SAFE.match(txt) else "unknown"

    def trip_halt(self, code: str) -> None:
        """Freeze every Robinhood write. Survives restart. Only a human clears it."""
        word = code if isinstance(code, str) and SAFE.match(code) else "halt"
        try:
            self.halt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.halt_path, "w", encoding="utf-8") as f:
                print(word, file=f)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass                                          # the audit line below still records it; halted() stays conservative
        self._audit("halt", code=word)

    def clear_halt(self) -> None:
        """Human action only (a UI route). Never wire this to an MCP tool and never call it automatically."""
        try:
            self.halt_path.unlink()
        except FileNotFoundError:
            pass
        self._audit("halt", code="cleared")

    def _audit(self, event: str, ticket: Optional[Ticket] = None, **fields) -> None:
        rec = {"ts": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds"), "event": event}
        if ticket is not None:
            fields = {"ticket": ticket.hash8, "symbol": ticket.symbol, "side": ticket.side, "qty": ticket.qty,
                      "limit_price": ticket.limit_price, **fields}
        for k in AUDIT_KEYS[2:]:
            v = fields.get(k)
            if v is None:
                continue
            rec[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else \
                (v if isinstance(v, str) and SAFE.match(v) else "?")
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                print(json.dumps(rec), file=f)
        except OSError:
            pass                                          # an audit failure never blocks a halt or a refusal

    # ---- intent and checks ------------------------------------------------------------------------------------
    @staticmethod
    def _norm(intent) -> dict:
        if not isinstance(intent, dict):
            raise Refused("bad_intent", "intent")
        sym, side = views.symbol(intent.get("symbol")), views.enum(intent.get("side"), views.SIDES)
        qty, px = intent.get("qty"), _num(intent.get("limit_price"))
        if sym is None or side is None:
            raise Refused("bad_intent", "symbol or side")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise Refused("bad_intent", "qty must be a whole number of shares")
        if px is None or px <= 0:
            raise Refused("bad_intent", "limit_price")
        return {"symbol": sym, "side": side, "qty": qty, "limit_price": px}

    def _live(self, t: Ticket, now: float) -> bool:
        return t.state in ACTIVE and not (t.state == PREVIEWED and now >= t.expires_at)

    async def _checks(self, it: dict, own: Optional[Ticket] = None) -> _Eval:
        """Every refusal that does not depend on the ticket itself. Re-run at the commit point by `place`."""
        lim, now = self.limits, self.clock()
        if not lim.trading:
            raise Refused("trading_off")
        if self.halted():
            raise Refused("halted")
        if lim.max_notional is None or lim.max_qty is None:
            raise Refused("cap_unset")
        if it["qty"] > lim.max_qty:
            raise Refused("qty_cap")
        notional = it["qty"] * it["limit_price"]
        reserved = sum(t.notional for t in self.tickets.values() if t is not own and self._live(t, now))
        if notional + reserved > lim.max_notional:
            raise Refused("notional_cap")
        if not self.market_open():
            raise Refused("market_closed")
        sym = it["symbol"]
        if any(t is not own and t.symbol == sym and self._live(t, now) for t in self.tickets.values()):
            raise Refused("symbol_busy", "a ticket for this symbol is live")
        try:
            with self.reader.cycle():
                try:
                    account = await self.reader.write_account()
                except McpError:
                    raise Refused("account_unknown") from None
                try:
                    setting = await self.reader.approval_setting()
                except McpError:
                    raise Refused("approvals_unknown") from None
                if setting.get("human_must_approve") is None:
                    raise Refused("approvals_unknown")
                if setting["human_must_approve"] is False:
                    raise Refused("approvals_off")
                q = await self._quote(sym)
                orders, approvals = await self._rows(self.reader.orders), await self._rows(self.reader.approvals)
                if any(o.get("symbol") == sym and o.get("status", "unknown") in WORKING for o in orders) or \
                        any(a.get("symbol") == sym and a.get("status") in ("pending", "unknown") for a in approvals):
                    raise Refused("symbol_busy", "a working order or pending approval exists")
                if it["side"] == "sell":
                    pos = await self._rows(self.reader.positions)
                    mine = [p for p in pos if p.get("symbol") == sym and p.get("asset") == "equity" and p.get("side") == "long"]
                    if not mine:
                        raise Refused("no_position")
                    held = mine[0].get("qty")
                    if held is None:
                        raise Refused("read_failed", "position size")
                    if held <= 0:
                        raise Refused("no_position")
                    if it["qty"] > held:
                        raise Refused("sell_exceeds_position")
        except Unknown:
            raise Refused("account_unknown") from None
        except McpError:
            raise Refused("read_failed") from None
        return _Eval(account=account)

    async def _rows(self, fn):
        try:
            return await fn()
        except McpError:
            raise Refused("read_failed") from None

    async def _quote(self, sym):
        try:
            q = await self.reader.quote(sym)
        except McpError:
            raise Refused("quote_stale", "quote unreadable") from None
        ts = None if q is None else q.get("updated_at")
        px = None if q is None else q.get("last")
        age = None if ts is None else self.clock() - ts
        if age is None or px is None or abs(age) > self.limits.quote_max_age_s:
            raise Refused("quote_stale")
        return q

    # ---- preview ---------------------------------------------------------------------------------------------
    async def preview(self, intent) -> Ticket:
        it = {}
        try:
            it = self._norm(intent)
            await self._checks(it)
        except Refused as r:
            self._audit("refuse", code=r.code, **it)
            raise
        ref = "px-" + secrets.token_hex(12)
        now = self.clock()
        t = Ticket(ticket_id=secrets.token_hex(8), ref_id=ref, hash8=_hash8(it, ref), created=now,
                   expires_at=now + self.limits.ticket_ttl_s, updated=now, **it)
        self.tickets[t.ticket_id] = t
        self._audit("preview", t, state=t.state)
        return t

    def discard(self, ticket_id: str) -> bool:
        """Drop an unplaced ticket so its symbol and notional free up. False if it is not a live preview."""
        t = self.tickets.get(ticket_id)
        if t is None or t.state != PREVIEWED:
            return False
        t.state = DISCARDED
        self._audit("discard", t, state=t.state)
        return True

    # ---- place -----------------------------------------------------------------------------------------------
    def _refuse_ticket(self, t: Ticket, r: Refused) -> Refused:
        if t.state == PREVIEWED:
            t.state = REFUSED                             # a refused place consumes the ticket: preview again
        self._audit("refuse", t, state=t.state, code=r.code)
        return r

    def _unknown(self, t: Ticket, code: str) -> dict:
        t.state = UNKNOWN
        self.trip_halt(code)
        self._audit("unknown", t, state=UNKNOWN, code=code)
        return {"outcome": "unknown", "state": t.state, "ticket": t.view()}

    async def place(self, ticket_id: str, confirm_hash: Optional[str] = None) -> dict:
        """Send exactly one order for a previewed ticket. Never retries, never replays. Raises `Refused` (the order was NOT
        sent); otherwise returns {outcome: awaiting_approval|placed|rejected|unknown, state, ticket}."""
        async with self._lock:
            t = self.tickets.get(ticket_id) if isinstance(ticket_id, str) else None
            if t is None:
                raise Refused("ticket_unknown")
            if t.state != PREVIEWED:
                raise Refused("ticket_used", "already " + t.state)
            if self.clock() >= t.expires_at:
                raise self._refuse_ticket(t, Refused("ticket_expired"))
            if confirm_hash is not None and confirm_hash.lower() != t.hash8:
                raise Refused("hash_mismatch")
            it = {"symbol": t.symbol, "side": t.side, "qty": t.qty, "limit_price": t.limit_price}
            try:
                ev = await self._checks(it, own=t)
                # The awaited reads above can outlast a halt, the ticket's life or the kill switch: re-check synchronously.
                if self.halted():
                    raise Refused("halted")
                if not self.limits.trading:
                    raise Refused("trading_off")
                if self.clock() >= t.expires_at:
                    raise Refused("ticket_expired")
                if t.state != PREVIEWED:
                    raise Refused("ticket_used")
            except Refused as r:
                raise self._refuse_ticket(t, r) from None
            # ---- commit point: from here a crash or timeout means UNKNOWN, never "probably not sent"
            t.state, t.updated = SENDING, self.clock()
            self._audit("place", t, state=SENDING)
            args = {"account_number": ev.account, "symbol": t.symbol, "side": t.side, "type": "limit",
                    "time_in_force": "gfd", "quantity": _fmt(t.qty), "limit_price": _fmt(t.limit_price), "ref_id": t.ref_id}
            try:
                data = await self.client.submit(PLACE_TOOL, args, _guard_key=_KEY)
            except McpError as e:
                if e.sent:
                    return self._unknown(t, "place_unknown")
                t.state, t.updated = REJECTED, self.clock()          # the server answered and did not act
                self._audit("reject", t, state=REJECTED, code=e.code if SAFE.match(e.code) else "rejected")
                return {"outcome": "rejected", "state": t.state, "ticket": t.view()}
            except BaseException:                       # includes cancellation mid-send: the outcome is unknown
                self._unknown(t, "place_unknown")
                raise
            return self._classify(t, data)

    def _classify(self, t: Ticket, data) -> dict:
        order = data.get("order") if isinstance(data, dict) and isinstance(data.get("order"), dict) else None
        raw = None if order is None else order.get("state", data.get("state"))
        state = raw.strip().lower() if isinstance(raw, str) else None
        ref = None if order is None else order.get("ref_id")
        if order is None or state is None or (ref is not None and ref != t.ref_id):
            return self._unknown(t, "unreadable_reply")
        t.order_id = views.ident(order.get("id")) or None
        t.approval_id = views.ident(order.get("approval_id") or data.get("approval_id")) or None
        t.updated = self.clock()
        if state == AWAITING:
            t.state = AWAITING
            self._audit("ack", t, state=AWAITING, order_id=t.order_id)
            return {"outcome": "awaiting_approval", "state": t.state, "ticket": t.view()}
        if state in ("rejected", "failed", "cancelled", "canceled"):
            t.state = REJECTED
            self._audit("reject", t, state=REJECTED, code="rejected")
            return {"outcome": "rejected", "state": t.state, "ticket": t.view()}
        t.state = PLACED                                # anything else may be a LIVE order: exposure, halt
        self.trip_halt("live_order")
        self._audit("ack", t, state=PLACED, code="live_order", order_id=t.order_id)
        return {"outcome": "placed", "state": t.state, "ticket": t.view()}

    def ticket_views(self) -> list:
        return [t.view() for t in self.tickets.values()]

