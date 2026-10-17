"""THE ORDER PATH. The only module that may reference an order-write endpoint.

Everything that can move money lives here, and nothing else in projectx/ may name these
endpoints or call PXClient.write (tests/test_px_guard_pin.py pins that). See docs/DECISIONS.md.

Flow: preview() runs every check and stages a hashed, single-use, short-lived Ticket; place()
(next increment) takes only a ticket id, so what was shown is byte-for-byte what is sent.
Every check fails closed: an unreadable, stale, missing or zero value refuses the open.
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from projectx_desk import sizing
from projectx_desk.client import PXError, PXRateLimited, PXWriteUnknown
from projectx_desk.limits import BRACKET_MODES, DayState, Limits, atomic_write_json
from projectx_desk.tradeday import CT

# ---- the only place these strings may appear -------------------------------------------------
ORDER_PLACE = "/api/Order/place"
ORDER_MODIFY = "/api/Order/modify"
ORDER_CANCEL = "/api/Order/cancel"
POSITION_CLOSE = "/api/Position/closeContract"
POSITION_PARTIAL_CLOSE = "/api/Position/partialCloseContract"

CONTRACT_RE = re.compile(r"^CON\.[A-Za-z0-9._]{1,40}$")
SAFE_AUDIT = re.compile(r"^[A-Za-z0-9_.:\-]{0,64}$")
AUDIT_KEYS = ("event", "ticket", "origin", "root", "side", "size", "risk_usd", "check", "code", "sha8", "state")
SESSION_OPEN = dtime(17, 0)

STAGED, PLACING, PLACED = "STAGED", "PLACING", "PLACED"
REJECTED, EXPIRED, SUPERSEDED, UNKNOWN = "REJECTED", "EXPIRED", "SUPERSEDED", "UNKNOWN"
RESOLVED = "RESOLVED"        # a human checked the platform and closed an UNKNOWN ticket
REJECTED_LIVE_S = 60.0       # a rejection that carried an order id may still fill, but only for this long
# Persisted intent states that still block new opens until reconciled or resolved by a human.
PENDING_INTENT = ("SENDING", "UNKNOWN", "NOT_FOUND")


class GuardError(Exception):
    pass


@dataclass
class Ticket:
    ticket_id: str
    contract_id: str
    root: str
    side: str                       # long | short
    origin: str                     # ui | mcp
    payload: dict                   # the exact entry body that will be POSTed
    stop_plan: dict                 # how the stop is attached (never guessed)
    sha256: str
    custom_tag: str
    readback: "OrderedDict[str, object]"
    sizing: "OrderedDict[str, object]"
    verdict: list                   # [(check, ok, detail)]
    created: float
    expires: float
    state: str = STAGED
    broker_order_id: Optional[int] = None
    acked: bool = False
    filled_size: int = 0
    stop_state: Optional[str] = None      # None | SENDING | ACKED | FAILED
    stop_order_id: Optional[int] = None
    stop_size: int = 0
    stop_current: Optional[float] = None   # where the working stop is NOW (starts at the staged stop, only tightens)
    risk_requested: float = 0.0
    stop_price: float = 0.0
    order_type: str = "market"
    entry_limit: Optional[float] = None
    rejected_at: float = 0.0        # clock() when a rejection carrying an order id came back
    acked_at: float = 0.0           # clock() when the broker ACKed the entry (in-flight exposure window)
    position_id: Optional[int] = None   # the broker position this ticket's fill created: evidence of ownership if its stop vanishes
    abandoned: bool = False         # the naked-position path flattened this ticket; a late stop ACK must be cancelled
    account_suffix: str = ""        # sensitive: confirm screen only, never in the AI surface or audit

    @property
    def hash8(self):
        return self.sha256[:8]


@dataclass
class Preview:
    ok: bool
    ticket: Optional[Ticket]
    verdict: list
    sizing: Optional[dict]
    placeable: bool                 # False while PX_TRADING=0 even if every check passed

    @property
    def reasons(self):
        return [f"{n}: {d}" for n, ok, d in self.verdict if not ok]


@dataclass
class PlaceResult:
    ok: bool
    state: str
    order_id: Optional[int] = None
    acked: bool = False              # the broker ACCEPTED the order. This is NOT a fill.
    error_code: Optional[int] = None
    reasons: tuple = ()


@dataclass
class MoveResult:
    ok: bool
    stop_price: Optional[float] = None
    reason: str = ""


@dataclass
class FlattenResult:
    flat: bool
    closed: tuple = ()
    orphan_orders: int = 0           # working orders left behind (live accounts cannot cancel via API)
    reasons: tuple = ()


@dataclass
class Eval:
    verdict: list
    d: Optional[dict]
    rec: Optional[dict]
    spec: object
    entry_px: Optional[float]
    simulated: Optional[bool]

    @property
    def ok(self):
        return self.d is not None and all(ok for _, ok, _ in self.verdict)


def _expiry_code(contract_id: str) -> str:
    """'CON.F.US.MES.Z26' -> 'Z26'. Built from the id, never from the gateway's free-text name field."""
    last = str(contract_id).split(".")[-1]
    return last if re.fullmatch(r"[A-Z]\d{2}", last) else ""


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _finite_pos(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


class Guard:
    def __init__(self, client, live, limits: Limits, user_hub=None, contract_lookup: Optional[Callable] = None,
                 clock=time.monotonic, wall=lambda: datetime.now(timezone.utc), day: Optional[DayState] = None,
                 sleep=asyncio.sleep, verify_tries: int = 10, verify_interval: float = 1.0):
        self.client, self.live, self.limits, self.user_hub = client, live, limits, user_hub
        self._lookup = contract_lookup or self._default_lookup
        self.clock, self.wall = clock, wall
        self.day = day or DayState(limits.state_path)
        self.tickets: dict[str, Ticket] = {}
        self._contracts: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()             # serialises place(): check + send is one critical section
        self._stop_lock = asyncio.Lock()        # one stop modification at a time
        self._flatten_lock = asyncio.Lock()     # flatten must never wait behind a hung place or stop write
        self._intents, self._intents_ok = self._load_intents()
        self.sleep, self.verify_tries, self.verify_interval = sleep, verify_tries, verify_interval
        self._watchdogs: dict[str, asyncio.Task] = {}
        self.protection: dict = {}
        if hasattr(live, "listen"):
            live.listen(self._on_live_event)

    # ---- helpers ----------------------------------------------------------------------------

    async def _default_lookup(self, contract_id):
        return await self.client.contract_by_id(contract_id)

    async def _contract(self, contract_id):
        hit = self._contracts.get(contract_id)
        if hit and self.clock() - hit[0] < 30:
            return hit[1]
        rec = await self._lookup(contract_id)
        if isinstance(rec, dict):
            self._contracts[contract_id] = (self.clock(), rec)
        return rec

    def halted(self) -> bool:
        try:
            return self.limits.halt_path.exists()
        except OSError:
            return True  # unreadable halt file is treated as halted

    def trip_halt(self, code: str) -> None:
        """Freeze all opens. Survives restart. Only a human clears it (clear_halt, UI-only)."""
        p = self.limits.halt_path
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            f.write(code if SAFE_AUDIT.match(code) else "halt")
            f.flush()
            os.fsync(f.fileno())
        self._audit("halt", code=code)

    def clear_halt(self) -> None:
        """Human action only. Never wire this to an MCP tool."""
        try:
            self.limits.halt_path.unlink()
        except FileNotFoundError:
            pass
        self._audit("halt", code="cleared")

    def _audit(self, event: str, **fields) -> None:
        """Append-only JSONL from a fixed key allowlist. No tokens, account ids or free text."""
        rec = {"ts": self.wall().isoformat(timespec="seconds"), "event": event}
        for k in AUDIT_KEYS[1:]:
            v = fields.get(k)
            if v is None:
                continue
            rec[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else \
                (v if isinstance(v, str) and SAFE_AUDIT.match(v) else "?")
        try:
            self.limits.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.limits.audit_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass  # audit failure must not block a reducing action; opens are checked elsewhere

    def _load_intents(self):
        try:
            raw = json.loads(self.limits.intent_path.read_text())
            return (raw if isinstance(raw, dict) else {}), isinstance(raw, dict)
        except FileNotFoundError:
            return {}, True
        except (ValueError, OSError):
            return {}, False  # unreadable intent log is unknown state: opens are refused

    def _set_intent(self, tag: str, **fields) -> None:
        rec = self._intents.setdefault(tag, {})
        rec.update(fields)
        atomic_write_json(self.limits.intent_path, self._intents)

    def _pending_intents(self):
        return [t for t, r in self._intents.items() if r.get("state") in PENDING_INTENT]

    def _open_contracts(self, root: str):
        by_root = total = 0
        for p in self.live.positions.values():
            n = abs(p.get("size") or 0)
            total += n
            try:
                r = sizing.spec_for(p.get("contractId", "")).root
            except sizing.SizingError:
                r = None
            if r == root:
                by_root += n
        return by_root, total

    IN_FLIGHT_S = 10.0

    def _ticket_exposed(self, t: Ticket) -> bool:
        """True while this ticket may still hold or create exposure, judged from broker-fed live state."""
        if t.state in (PLACING, UNKNOWN):
            return True
        if t.state != PLACED:
            return False
        if getattr(t, "filled_size", 0) and any(abs(p.get("size") or 0) and p.get("contractId") == t.contract_id
                                 for p in self.live.positions.values()):
            return True
        if getattr(t, "broker_order_id", None) is not None and t.broker_order_id in self.live.orders:
            return True
        return self.clock() - getattr(t, "acked_at", 0.0) < self.IN_FLIGHT_S      # ACKed a moment ago: the hub may not have caught up

    def _open_risk(self) -> float:
        """Worst-case dollars already committed by orders that may be live (their staged risk, never reduced)."""
        return sum(float((getattr(t, "readback", None) or {}).get("risk_usd") or 0.0)
                   for t in self.tickets.values() if self._ticket_exposed(t))

    def _contract_busy(self, contract_id: str) -> bool:
        """A second entry in a contract that has a position, a working order or an entry in flight is refused:
        one protection owner per net position, so stops can never add up to more than the position."""
        if any(p.get("contractId") == contract_id and abs(p.get("size") or 0) for p in self.live.positions.values()):
            return True
        if any(o.get("contractId") == contract_id for o in self.live.orders.values()):
            return True
        return any(getattr(t, "contract_id", None) == contract_id and self._ticket_exposed(t) for t in self.tickets.values())

    def _past_cutoff(self, now_ct) -> Optional[str]:
        cut = self.limits.no_new_after
        if not cut:
            return "PX_NO_NEW_AFTER is not set"
        try:
            hh, mm = cut.split(":")
            cutoff = dtime(int(hh), int(mm))
        except (ValueError, AttributeError):
            return "PX_NO_NEW_AFTER is not HH:MM"
        if cutoff > SESSION_OPEN:
            return "cutoff must be at or before the 17:00 CT session open"
        t = now_ct.time()
        why = self._flat_by_window(now_ct)
        if why:
            return why
        if t >= SESSION_OPEN or t < cutoff:
            return None
        return f"no new entries after {cut} CT"

    FLAT_LEAD = timedelta(minutes=2)

    def _flat_by_base(self) -> Optional[dtime]:
        fb = self.limits.flat_by
        if not fb:
            return None
        try:
            hh, mm = fb.split(":")
            t = dtime(int(hh), int(mm))
        except (ValueError, AttributeError):
            return None
        return t if t < SESSION_OPEN else None

    def _early_close(self, day) -> Optional[dtime]:
        """PX_EARLY_CLOSES ("YYYY-MM-DD=HH:MM,...", CT): the flatten time to use on a holiday or early-close date. Entries are
        YOURS, copied from Topstep's calendar (the lead time before an early close has changed between their articles): the
        desk ships no calendar of its own. A garbled entry is ignored, never guessed."""
        for part in (self.limits.early_closes or "").split(","):
            k, _, v = part.strip().partition("=")
            try:
                d = datetime.strptime(k.strip(), "%Y-%m-%d").date()
                hh, mm = v.strip().split(":")
                t = dtime(int(hh), int(mm))
            except (ValueError, AttributeError):
                continue
            if d == day and t < SESSION_OPEN:
                return t
        return None

    def _flat_by_time(self, now_ct=None) -> Optional[dtime]:
        """The flatten time in force: PX_FLAT_BY, or an earlier one on a date listed in PX_EARLY_CLOSES."""
        base = self._flat_by_base()
        ec = self._early_close(now_ct.date()) if now_ct is not None else None
        if ec is None:
            return base
        return ec if base is None else min(base, ec)

    def _flat_by_window(self, now_ct) -> Optional[str]:
        """A configured PX_FLAT_BY must parse (else every open is refused), and no new entry is allowed in the last 2 minutes."""
        if self.limits.flat_by and self._flat_by_base() is None:
            return "PX_FLAT_BY is not HH:MM at or before 17:00 CT"
        fb = self._flat_by_time(now_ct)
        if fb is None:
            return None
        start = (datetime.combine(now_ct.date(), fb) - self.FLAT_LEAD).time()
        t = now_ct.time()
        if start <= t < SESSION_OPEN:
            return f"inside the last 2 minutes before the {fb.strftime('%H:%M')} CT flat-by deadline"
        return None

    def flat_by_due(self, now=None) -> bool:
        """True from PX_FLAT_BY until the 17:00 CT session open: everything must be flat."""
        now_ct = (now or self.wall()).astimezone(CT)
        fb = self._flat_by_time(now_ct)
        if fb is None:
            return False
        return fb <= now_ct.time() < SESSION_OPEN

    async def enforce_flat_by(self, now=None):
        """Called about once a second by the app. At the deadline, flatten any position or working order. Reduces only."""
        if not self.flat_by_due(now):
            return None
        has_pos = any(abs(p.get("size") or 0) for p in self.live.positions.values())
        if not has_pos and not self.live.orders:
            return None
        self._audit("flatten", origin="deadline", state="DEADLINE")
        return await self.flatten(origin="deadline")

    # ---- shared budget and stop helpers (one definition, used by the UI, MCP and digest) -----------

    def dd_room(self) -> Optional[float]:
        """Drawdown room in dollars, or None if unknown. With PX_MLL_FLOOR it is the LIVE balance minus the firm's floor
        (so it follows the account); otherwise the typed PX_DRAWDOWN_BUFFER. A floor with an unreadable balance is None."""
        lim = self.limits
        if lim.mll_floor > 0:
            bal = (self.live.account or {}).get("balance")
            if not isinstance(bal, (int, float)) or isinstance(bal, bool) or not math.isfinite(bal):
                return None
            return float(bal) - lim.mll_floor
        return lim.dd_buffer if lim.dd_buffer > 0 else None

    def open_pnl(self) -> Optional[float]:
        """Unrealised P&L of every open position in dollars, marked at the last trade of a fresh quote. None means UNKNOWN,
        never zero: any position that cannot be valued (no quote younger than PX_MAX_HUB_AGE_S, no contract record, a
        PositionType that is not 1 or 2) makes the whole figure None, and a caller must refuse to open on None.

        Why: the firm watches the maximum-loss limit on equity INCLUDING open P&L (docs/TOPSTEP_RULES.md), while the balance
        the gateway reports moves on fills only. A room built from realised P&L alone can read 'locked=NO' while a large open
        loser is liquidating the account. Round 10."""
        total = 0.0
        for p in self.live.positions.values():
            size = p.get("size")
            if not isinstance(size, (int, float)) or isinstance(size, bool) or size == 0:
                continue
            kind, avg, cid = p.get("type"), p.get("averagePrice"), p.get("contractId")
            hit = self._contracts.get(cid)
            rec = hit[1] if hit else None
            q = self.live.quotes.get(cid) or {}
            last = q.get("lastPrice")
            if kind not in (1, 2) or isinstance(kind, bool) or not _finite_pos(avg) or not isinstance(rec, dict) \
                    or not _finite_pos(rec.get("tickSize")) or not _finite_pos(rec.get("tickValue")) or not _finite_pos(last) \
                    or self.clock() - q.get("_rx", float("-inf")) > self.limits.max_hub_age_s:
                return None
            direction = 1.0 if kind == 1 else -1.0
            total += (float(last) - float(avg)) * direction * abs(float(size)) / float(rec["tickSize"]) * float(rec["tickValue"])
        return round(total, 2)

    async def mark_positions(self) -> None:
        """Fetch what open_pnl() needs for every open position: the contract record and a fresh quote. Never raises; a
        failed lookup simply leaves that position unvalued, and open_pnl() then says UNKNOWN."""
        for cid in {p.get("contractId") for p in self.live.positions.values() if p.get("size")}:
            if not isinstance(cid, str) or not CONTRACT_RE.match(cid):
                continue
            try:
                await self._contract(cid)
            except Exception:  # noqa: BLE001
                pass
            if hasattr(self.live, "ensure_quote"):
                await self.live.ensure_quote(cid, wait=0.5)

    def draw(self) -> Optional[float]:
        """Dollars already spoken for against the loss room: the WORSE of the stop risk committed by our live tickets and the
        unrealised loss showing right now. A position in profit draws nothing extra (a gain is not banked until it closes).
        None if a position is open and its P&L is UNKNOWN."""
        ob = self._open_risk()
        if not any(p.get("size") for p in self.live.positions.values()):
            return ob
        op = self.open_pnl()
        if op is None:
            return None
        return max(ob, -min(op, 0.0))

    def budget(self, now=None):
        """(remaining, daily_room) in dollars, or (None, None) if P&L, limits or the value of an open position are unknown.
        Fails closed. Both rooms are net of draw(): committed stop risk or the open loss, whichever is worse."""
        lim = self.limits
        pnl = self.live.day_pnl(now or self.wall())
        dd = self.dd_room()
        if pnl is None or lim.daily_loss_limit <= 0 or dd is None:
            return None, None
        dr = self.draw()
        if dr is None:
            return None, None
        room = lim.daily_loss_limit + pnl - dr
        return min(room, dd - dr), room

    def risk_dollars_for(self, risk_pct: float, now=None) -> float:
        """1R in dollars. Base (PX_RISK_BASE=drawdown, default): risk_pct of the live drawdown room (balance minus the MLL
        floor): the firm's loss limit is the base, not the account size, and it shrinks as you lose. Never above the remaining
        daily or drawdown room. Legacy base: risk_pct of the remaining budget. Unreadable inputs fail closed to 0."""
        remaining, room = self.budget(now)
        if remaining is None or remaining <= 0:
            return 0.0
        dd = self.dd_room() or 0.0
        base = dd if self.limits.risk_base == "drawdown" else remaining
        return sizing.risk_budget(base, risk_pct, room, dd)

    async def stop_from_ticks(self, contract_id: str, side: str, ticks: int):
        """Absolute stop price `ticks` away from the FRESH quote the guard will size from. None if unknowable.

        The UI sends ticks, not a price, so a market that moved between the size box and STAGE cannot
        silently change the stop distance the trader saw.
        """
        try:
            rec = await self._contract(contract_id)
            q = self.live.quotes.get(contract_id) or {}
            ref = q.get("bestAsk" if side == "long" else "bestBid") or q.get("lastPrice")
            tick = float(rec["tickSize"])
            if not (_finite_pos(ref) and _finite_pos(tick) and isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 1):
                return None
            px = Decimal(str(ref)) - Decimal(ticks) * Decimal(str(tick)) if side == "long" \
                else Decimal(str(ref)) + Decimal(ticks) * Decimal(str(tick))
            return float(px) if px > 0 else None
        except Exception:  # noqa: BLE001
            return None

    def unreconciled(self) -> bool:
        """True while any write is UNKNOWN or an intent is unresolved (the desk shows this loudly)."""
        return (not self._intents_ok) or bool(self._pending_intents()) or any(t.state == UNKNOWN for t in self.tickets.values())

    def behavior(self, now=None) -> dict:
        """Behavioral rails from today's closed trades. They only ever REFUSE or REDUCE; they never allow more.

        cooldown   no new opens for a while after a losing trade (revenge-trade friction)
        locked     N losses in a row ends the session for new opens
        risk_cap   after a loss, risk may not exceed what the losing trade risked (never size up to chase)
        """
        lim, now = self.limits, now or self.wall()
        closed = self.live.closed_trades(now)
        if closed is None:
            return {"known": False, "consecutive_losses": None, "cooldown_left_s": 0, "locked": False, "risk_cap": None,
                    "lockout_after": lim.max_consecutive_losses}
        consec = 0
        for _, pnl in reversed(closed):
            if pnl < 0:
                consec += 1
            else:
                break
        last = closed[-1] if closed else None
        left = 0
        if last and last[1] < 0 and lim.cooldown_after_loss_s > 0:
            left = max(0, int(round(lim.cooldown_after_loss_s - (now - last[0]).total_seconds())))
        cap = self.day.last_risk_today(now) if (lim.size_after_loss_lock and last and last[1] < 0) else None
        return {"known": True, "consecutive_losses": consec, "cooldown_left_s": left, "lockout_after": lim.max_consecutive_losses,
                "locked": bool(lim.max_consecutive_losses > 0 and consec >= lim.max_consecutive_losses), "risk_cap": cap}

    # ---- shared evaluation (preview and place run the SAME checks) ---------------------------

    async def _evaluate(self, *, contract_id, side, stop_price, risk_dollars, order_type, entry, origin) -> Eval:
        lim, v = self.limits, []

        def chk(name, ok, detail=""):
            v.append((name, bool(ok), detail if not ok else ""))
            return bool(ok)

        side = (side or "").lower()
        chk("inputs", side in ("long", "short") and origin in ("ui", "mcp") and CONTRACT_RE.match(contract_id or "")
            and order_type in ("market", "limit") and _finite_pos(stop_price) and _finite_pos(risk_dollars),
            "side/origin/contract/order_type/stop/risk invalid")
        if not v[-1][1]:
            return Eval(v, None, None, None, None, None)

        chk("halt", not self.halted(), "halted: a human must clear it")
        chk("day_state", self.day.ok, "day-state file is corrupt")

        acct = self.live.account or {}
        chk("account_bound", lim.account_id is not None and self.live.account_id == lim.account_id,
            "PX_ACCOUNT_ID unset or does not match the connected account")
        chk("account_tradable", acct.get("canTrade") is True, "account cannot trade")
        simulated = acct.get("simulated")
        if lim.account_kind == "sim":
            chk("account_kind", simulated is True, "config says sim but the account is not simulated")
        else:
            chk("account_kind", lim.allow_live and simulated is False, "live account needs PX_ALLOW_LIVE=1")

        hub_age = self.user_hub.feed_age_s() if self.user_hub else None
        chk("user_hub", bool(self.user_hub and self.user_hub.up) and hub_age is not None and hub_age <= lim.max_hub_age_s
            and self.live.positions_seeded and self.live.trades_seeded,
            "user hub down, stale or state not seeded from REST")

        try:
            rec = await self._contract(contract_id)
        except Exception:  # noqa: BLE001 - any lookup failure refuses
            rec = None
        spec = None
        if chk("contract_record", isinstance(rec, dict) and rec.get("activeContract") is True
               and _finite_pos(rec.get("tickSize")) and _finite_pos(rec.get("tickValue")),
               "contract unknown, not the active contract, or tick data missing"):
            try:
                spec = sizing.spec_for(contract_id, rec["tickSize"], rec["tickValue"])
            except sizing.SizingError as e:
                chk("contract_spec", False, str(e))
        if spec is not None:
            chk("allowed_root", spec.root in lim.allowed_roots, f"{spec.root} is not in PX_ALLOWED_ROOTS")

        # price reference: market orders are sized off the side that would fill
        q = self.live.quotes.get(contract_id)
        ref = None
        if chk("quote", isinstance(q, dict) and self.clock() - q.get("_rx", -1e9) <= lim.max_quote_age_s,
               "no quote or older than the freshness limit"):
            ref = q.get("bestAsk" if side == "long" else "bestBid") or q.get("lastPrice")
            chk("quote_price", _finite_pos(ref), "quote has no usable price")
        entry_px = float(ref) if _finite_pos(ref) else None
        if order_type == "limit":
            chk("limit_allowed", lim.account_kind == "sim", "limit opens are sim-only in v1")
            if chk("limit_entry", _finite_pos(entry), "limit order needs an entry price") and entry_px and spec:
                dev = abs(Decimal(str(entry)) - Decimal(str(entry_px))) / Decimal(str(spec.tick_size))
                chk("entry_deviation", dev <= lim.max_entry_dev_ticks, "limit entry too far from the market")
                entry_px = float(entry)

        if spec and entry_px:
            try:
                ticks_dec = abs(Decimal(str(entry_px)) - Decimal(str(stop_price))) / Decimal(str(spec.tick_size))
                aligned = (Decimal(str(stop_price)) / Decimal(str(spec.tick_size))) % 1 == 0
            except ArithmeticError:                 # an absurd stop price overflows Decimal: refuse it, never raise
                ticks_dec, aligned = Decimal(10) ** 9, False
            chk("stop_aligned", aligned, "stop is not on a tick")
            chk("stop_side", (side == "long" and stop_price < entry_px) or (side == "short" and stop_price > entry_px),
                "stop is on the wrong side of entry")
            chk("stop_distance", lim.min_stop_ticks <= ticks_dec <= lim.max_stop_ticks,
                f"stop must be {lim.min_stop_ticks} to {lim.max_stop_ticks} ticks away")

        if chk("bracket_mode", lim.bracket_mode in BRACKET_MODES, "PX_BRACKET_MODE is unset: opens refused"):
            if lim.account_kind == "live":
                chk("bracket_live", lim.bracket_mode == "oco" and order_type == "market",
                    "live opens need native OCO brackets and a market entry")

        now = self.wall()
        why = self._past_cutoff(now.astimezone(CT))
        chk("session_window", why is None, why or "")

        await self.mark_positions()                 # open P&L needs each position's contract record and a fresh quote
        pnl = self.live.day_pnl(now)
        chk("day_pnl_known", pnl is not None, "today's P&L is unknown (trades not read from REST)")
        dd = self.dd_room()
        chk("limits_configured", lim.daily_loss_limit > 0 and (lim.dd_buffer > 0 or lim.mll_floor > 0),
            "PX_DAILY_LOSS_LIMIT and PX_DRAWDOWN_BUFFER (or PX_MLL_FLOOR) must be set")
        chk("drawdown_known", lim.mll_floor <= 0 or dd is not None, "PX_MLL_FLOOR is set but the account balance is unreadable")
        remaining = None
        if pnl is not None and lim.daily_loss_limit > 0 and dd is not None:
            dr = self.draw()
            if chk("open_pnl_known", dr is not None,
                   "an open position cannot be valued (no fresh quote or contract record), so the loss room is unknown"):
                remaining = min(lim.daily_loss_limit + pnl - dr, dd - dr)
                chk("budget_remaining", remaining > 0, "no loss room left today")
        chk("orders_today", self.day.orders_today(now) < lim.max_orders_day, "daily order count reached")
        b = self.behavior(now)
        chk("cooldown_after_loss", b["cooldown_left_s"] == 0,
            f"cooldown after a loss: {b['cooldown_left_s'] // 60}m{b['cooldown_left_s'] % 60:02d}s left. Walk away.")
        chk("loss_lockout", not b["locked"], f"locked for the session: {b['consecutive_losses']} losses in a row")
        chk("contract_flat", not self._contract_busy(contract_id),
            "this contract already has a position, a working order or an entry in flight")
        chk("no_unknown", self._intents_ok and not self._pending_intents()
            and not any(t.state == UNKNOWN for t in self.tickets.values()),
            "an earlier order is UNKNOWN or unreconciled")

        d = None
        if spec and entry_px and remaining and remaining > 0 and all(ok for _, ok, _ in v):
            by_root, total = self._open_contracts(spec.root)
            headroom = min(lim.max_qty_order, lim.max_qty_symbol - by_root, lim.max_qty_total - total)
            if chk("exposure_headroom", headroom >= 1, "open-contract caps leave no room"):
                eff_risk = min(float(risk_dollars), lim.max_loss_frac * remaining)
                if b["risk_cap"]:                       # after a loss: never risk more than the losing trade did
                    eff_risk = min(eff_risk, b["risk_cap"])
                try:
                    d = sizing.describe_trade(contract_id, side, entry_px, stop_price, eff_risk,
                                              risk_budget_dollars=remaining, max_contracts=headroom,
                                              live_tick_size=rec["tickSize"], live_tick_value=rec["tickValue"])
                except sizing.SizingError as e:
                    chk("sizing", False, str(e))
                if d is not None:
                    chk("sizing", not d["refused"], str(d["reason"]))
                    if not d["refused"]:
                        chk("budget_multiple", remaining >= lim.min_budget_multiple * d["total_risk_dollars"],
                            "remaining budget is under the safety multiple of this trade's max loss")
        return Eval(v, d, rec, spec, entry_px, simulated)

    # ---- preview ----------------------------------------------------------------------------

    async def preview(self, *, contract_id: str, side: str, stop_price: float, risk_dollars: float,
                      order_type: str = "market", entry: Optional[float] = None, origin: str = "ui") -> Preview:
        """Run every check; stage a ticket only if all pass. Never raises on a refusal."""
        if origin == "mcp" and any(t.state == STAGED and t.origin == "ui" and self.clock() < t.expires
                                   for t in self.tickets.values()):
            return self._refuse([("ui_ticket_pending", False, "a ticket staged at the desk is waiting; the desk owns it")], origin)
        if isinstance(contract_id, str) and CONTRACT_RE.match(contract_id) and hasattr(self.live, "ensure_quote"):
            await self.live.ensure_quote(contract_id)
        ev = await self._evaluate(contract_id=contract_id, side=side, stop_price=stop_price, risk_dollars=risk_dollars,
                                  order_type=order_type, entry=entry, origin=origin)
        if not ev.ok:
            return self._refuse(ev.verdict, origin, ev.d)
        return self._stage(ev, (side or "").lower(), order_type, entry, stop_price, risk_dollars, origin, contract_id)

    def _refuse(self, v, origin, d=None):
        failed = next((n for n, ok, _ in v if not ok), None)
        self._audit("reject", origin=origin, check=failed)
        return Preview(False, None, v, dict(d) if d else None, False)

    def _stage(self, ev, side, order_type, entry, stop_price, risk_requested, origin, contract_id):
        lim, d, rec, spec = self.limits, ev.d, ev.rec, ev.spec
        now_m = self.clock()
        for t in self.tickets.values():           # restaging supersedes; one live intent at a time
            if t.state == STAGED:
                t.state = SUPERSEDED
        tid = str(uuid.uuid4())
        size = int(d["contracts"])
        body = {"accountId": self.live.account_id, "contractId": contract_id,
                "type": 2 if order_type == "market" else 1, "side": 0 if side == "long" else 1, "size": size,
                "limitPrice": ev.entry_px if order_type == "limit" else None, "stopPrice": None, "trailPrice": None}
        stop_ticks = int(d["stop_ticks"])
        if lim.bracket_mode == "oco":
            body["stopLossBracket"] = {"ticks": stop_ticks, "type": 4}
            body["takeProfitBracket"] = None
            stop_plan = {"mode": "oco", "ticks": stop_ticks}
        else:
            stop_plan = {"mode": "separate_after_fill", "stop_price": stop_price,
                         "side": 1 if side == "long" else 0, "unprotected_until": "stop acknowledged"}
        h = hashlib.sha256((canonical(body) + tid).encode()).hexdigest()[:8]
        tag = f"px-{h}-1"
        body["customTag"] = tag
        digest = hashlib.sha256(canonical({"entry": body, "stop": stop_plan}).encode()).hexdigest()
        readback = OrderedDict([
            ("account_kind", "SIM" if ev.simulated else "LIVE"),
            ("contract", f"{spec.root} {_expiry_code(contract_id)}".strip()),
            ("side", "BUY (long)" if side == "long" else "SELL (short)"),
            ("size", size), ("order_type", order_type.upper()), ("entry_ref", ev.entry_px),
            ("stop_price", stop_price), ("stop_ticks", stop_ticks),
            ("risk_usd", d["total_risk_dollars"]), ("pct_of_budget", round(d["pct_of_risk_budget_used"], 1)),
            ("binding", d["binding_constraint"]),
            ("stop_plan", stop_plan["mode"].upper()),
            ("protection", "ATOMIC" if lim.bracket_mode == "oco" else "UNPROTECTED UNTIL STOP ACKED"),
        ])
        t = Ticket(ticket_id=tid, contract_id=contract_id, root=spec.root, side=side, origin=origin, payload=body,
                   stop_plan=stop_plan, sha256=digest, custom_tag=tag, readback=readback, sizing=d, verdict=ev.verdict,
                   created=now_m, expires=now_m + lim.ticket_ttl_s, risk_requested=float(risk_requested),
                   stop_price=float(stop_price), order_type=order_type,
                   entry_limit=float(entry) if order_type == "limit" else None,
                   account_suffix=str(self.live.account_id)[-3:])
        self.tickets[tid] = t
        self._audit("preview", ticket=t.hash8, origin=origin, root=spec.root, side=side, size=size,
                    risk_usd=d["total_risk_dollars"], sha8=t.hash8)
        placeable = lim.trading and (origin != "mcp" or lim.mcp_place)
        return Preview(True, t, ev.verdict, dict(d), placeable)

    # ---- place ------------------------------------------------------------------------------

    def _deny(self, t, reasons, origin="ui", state=None):
        self._audit("reject", ticket=t.hash8 if t else None, origin=origin, state=state,
                    check=(reasons[0].split(":")[0] if reasons else None))
        return PlaceResult(False, state or (t.state if t else "NONE"), reasons=tuple(reasons))

    async def _rest_matches_live(self) -> Optional[str]:
        """Fresh REST re-read: the guard may only open from a picture the broker agrees with."""
        try:
            pos = await self.client.open_positions(self.live.account_id)
            ords = await self.client.open_orders(self.live.account_id)
        except Exception:  # noqa: BLE001 - cannot verify state, so refuse
            return "fresh_state: could not re-read positions/orders"
        rest_p = {p["id"]: abs(p.get("size") or 0) for p in pos}
        live_p = {k: abs(v.get("size") or 0) for k, v in self.live.positions.items()}
        rest_o = {o["id"] for o in ords}
        if rest_p != live_p or rest_o != set(self.live.orders):
            return "fresh_state: broker positions/orders differ from the desk's view"
        return None

    def _commit_gate(self, t, ev) -> list:
        """Synchronous last look before the entry write. Returns refusal reasons (empty = go)."""
        lim, out = self.limits, []
        if t.state != STAGED:
            return [f"recheck: ticket is {t.state} (restaged or consumed while placing)"]
        if self.halted():
            out.append("recheck: halted during the broker read")
        if self.clock() >= t.expires:
            out.append("recheck: ticket expired during the broker read")
        hub_age = self.user_hub.feed_age_s() if self.user_hub else None
        if not (self.user_hub and self.user_hub.up and hub_age is not None and hub_age <= lim.max_hub_age_s
                and self.live.positions_seeded and self.live.trades_seeded):
            out.append("recheck: user hub down, stale or unseeded: fills and protection could not be observed")
        q = self.live.quotes.get(t.contract_id)
        if not isinstance(q, dict) or self.clock() - q.get("_rx", -1e9) > lim.max_quote_age_s:
            out.append("recheck: quote stale during the broker read")
        elif t.order_type == "market":
            ref = q.get("bestAsk" if t.side == "long" else "bestBid") or q.get("lastPrice")
            if not _finite_pos(ref):
                out.append("recheck: quote has no usable price")
            else:
                drift = abs(Decimal(str(ref)) - Decimal(str(t.readback["entry_ref"]))) / Decimal(str(ev.spec.tick_size))
                if drift > lim.max_drift_ticks:
                    out.append(f"recheck: market moved {drift} ticks during the broker read; restage")
        remaining, _daily = self.budget(self.wall())
        risk = t.readback.get("risk_usd")
        if remaining is None or (isinstance(risk, (int, float)) and remaining < lim.min_budget_multiple * risk):
            out.append("recheck: risk headroom shrank during the broker read")
        return out

    def _unknown(self, t, code):
        t.state = UNKNOWN
        self._set_intent(t.custom_tag, state="UNKNOWN")
        self.trip_halt("unknown_order")
        self._audit("unknown", ticket=t.hash8, state=UNKNOWN, code=code)
        return PlaceResult(False, UNKNOWN, reasons=("outcome unknown: halted; reconcile before anything else",))

    async def place(self, ticket_id: str, *, origin: str, confirm_hash: str) -> PlaceResult:
        """Send exactly one entry order for a staged ticket. Never retries, never replays."""
        lim = self.limits
        async with self._lock:
            t = self.tickets.get(ticket_id)
            if t is None:
                return self._deny(None, ["ticket: unknown"], origin)
            if t.state != STAGED:
                return self._deny(t, [f"ticket: already {t.state}"], origin)
            if self.clock() >= t.expires:
                t.state = EXPIRED
                return self._deny(t, ["ticket: expired"], origin, EXPIRED)
            if not isinstance(confirm_hash, str) or not hmac.compare_digest(confirm_hash.lower(), t.hash8):
                return self._deny(t, ["confirm: hash does not match the ticket shown"], origin)
            if hashlib.sha256(canonical({"entry": t.payload, "stop": t.stop_plan}).encode()).hexdigest() != t.sha256:
                t.state = REJECTED
                return self._deny(t, ["ticket: payload no longer matches its hash"], origin, REJECTED)
            if origin not in ("ui", "mcp"):
                return self._deny(t, ["origin: invalid"], origin)
            if origin == "mcp" and not lim.mcp_place:
                return self._deny(t, ["origin: MCP may not place (PX_MCP_PLACE=0)"], origin)
            if not lim.trading:
                return self._deny(t, ["kill_switch: PX_TRADING is off"], origin)

            ev = await self._evaluate(contract_id=t.contract_id, side=t.side, stop_price=t.stop_price,
                                      risk_dollars=t.risk_requested, order_type=t.order_type,
                                      entry=t.entry_limit, origin=origin)
            reasons = [f"{n}: {dtl}" for n, ok, dtl in ev.verdict if not ok]
            if ev.ok and ev.d["contracts"] < t.payload["size"]:
                reasons.append("size: headroom shrank since staging")
            if ev.ok and t.order_type == "market" and ev.entry_px is not None:
                drift = abs(Decimal(str(ev.entry_px)) - Decimal(str(t.readback["entry_ref"]))) \
                    / Decimal(str(ev.spec.tick_size))
                if drift > lim.max_drift_ticks:
                    reasons.append(f"drift: market moved {drift} ticks since staging; restage")
            if not reasons:
                mismatch = await self._rest_matches_live()
                if mismatch:
                    reasons.append(mismatch)
            if reasons:
                if t.state == STAGED:       # a ticket restaged during the awaits keeps its SUPERSEDED state
                    t.state = REJECTED      # a refused place consumes the ticket; restage against the new world
                return self._deny(t, reasons, origin, REJECTED)

            # Awaited reads above can outlast a halt, the ticket's life, the hub, the quote or a restage. Re-check EVERYTHING
            # the authorization depended on, synchronously, at the commit point (no await between here and the write).
            gate = self._commit_gate(t, ev)
            if gate:
                if t.state == STAGED:
                    t.state = REJECTED                  # a superseded ticket keeps its SUPERSEDED state
                return self._deny(t, gate, origin, t.state)

            # ---- commit point: from here a crash or timeout means UNKNOWN, never "probably not sent"
            try:
                self.day.count_order(self.wall(), risk=t.readback.get("risk_usd"))
                self._set_intent(t.custom_tag, state="SENDING", hash8=t.hash8, root=t.root, contract=t.contract_id, size=t.payload["size"],
                                 ts=self.wall().isoformat(timespec="seconds"))
            except OSError:
                t.state = REJECTED
                return self._deny(t, ["state: could not persist the order intent"], origin, REJECTED)
            t.state = PLACING
            self._audit("place", ticket=t.hash8, origin=origin, root=t.root, side=t.side,
                        size=t.payload["size"], sha8=t.hash8)
            try:
                resp = await self.client.write(ORDER_PLACE, t.payload)
            except (PXWriteUnknown, PXRateLimited):
                return self._unknown(t, "transport")
            except PXError:                    # connect failed / login failed: nothing was sent
                t.state = REJECTED
                self._set_intent(t.custom_tag, state="REJECTED")
                self._audit("reject", ticket=t.hash8, state=REJECTED, code="not_sent")
                return PlaceResult(False, REJECTED, reasons=("not sent",))
            except BaseException:              # includes cancellation mid-send: outcome unknown
                self._unknown(t, "cancelled")
                raise

            if not isinstance(resp, dict) or not isinstance(resp.get("success"), bool):
                return self._unknown(t, "malformed")
            oid = resp.get("orderId") if isinstance(resp.get("orderId"), int) else None
            code = resp.get("errorCode") if isinstance(resp.get("errorCode"), int) else None
            t.broker_order_id = oid
            if resp["success"] and oid is not None:
                t.state, t.acked, t.acked_at = PLACED, True, self.clock()
                self._set_intent(t.custom_tag, state="ACKED", order_id=oid)
                self._audit("ack", ticket=t.hash8, state=PLACED, code=str(code))
                return PlaceResult(True, PLACED, order_id=oid, acked=True, error_code=code)
            if resp["success"] is False:       # definitive refusal; the docs say the order may still exist (orderId kept)
                t.state, t.rejected_at = REJECTED, self.clock()
                self._set_intent(t.custom_tag, state="REJECTED", order_id=oid)
                self._audit("reject", ticket=t.hash8, state=REJECTED, code=str(code))
                return PlaceResult(False, REJECTED, order_id=oid, error_code=code, reasons=("broker rejected",))
            return self._unknown(t, "success_without_id")

    # ---- reconcile (read-only) --------------------------------------------------------------

    async def reconcile(self) -> dict:
        """For each unreconciled intent, search the broker's orders by our customTag.

        FOUND: the order exists (ticket becomes PLACED). NOT_FOUND: stays blocking; a human decides.
        Never resends. The halt file is NOT cleared here; clear_halt() is a human action.
        """
        out = {}
        for tag in self._pending_intents():
            rec = self._intents[tag]
            try:
                ts = datetime.fromisoformat(rec.get("ts", "")).astimezone(timezone.utc)
            except ValueError:
                ts = self.wall()
            try:
                orders = await self.client.search_orders(
                    self.live.account_id, (ts.replace(microsecond=0) - timedelta(minutes=5)).isoformat(),
                    (self.wall() + timedelta(minutes=1)).isoformat())
            except Exception:  # noqa: BLE001
                out[tag] = "ERROR"
                continue
            hit = next((o for o in orders if o.get("customTag") == tag), None)
            if hit:
                self._set_intent(tag, state="FOUND", order_id=hit.get("id"))
                for t in self.tickets.values():
                    if t.custom_tag == tag:
                        t.state, t.acked, t.broker_order_id = PLACED, True, hit.get("id")
                out[tag] = "FOUND"
            else:
                self._set_intent(tag, state="NOT_FOUND")
                out[tag] = "NOT_FOUND"
            self._audit("reconcile", code=out[tag], state=out[tag])
        return out

    def resolve_not_found(self) -> int:
        """Human action: close every intent reconcile could not find at the broker (after checking the platform).
        Only NOT_FOUND ones; an UNKNOWN or FOUND intent still needs a real answer. Returns how many were resolved."""
        n = 0
        for tag, rec in list(self._intents.items()):
            if rec.get("state") == "NOT_FOUND":
                self.resolve_intent(tag, "operator_checked_not_found")
                n += 1
        return n

    def resolve_intent(self, tag: str, note: str = "resolved") -> None:
        """Human action: mark an unreconciled intent as dealt with (e.g. after checking TopstepX)."""
        if tag in self._intents:
            self._set_intent(tag, state="RESOLVED")
            for t in self.tickets.values():
                if t.custom_tag == tag and t.state == UNKNOWN:
                    t.state = RESOLVED
            self._audit("reconcile", code=note, state="RESOLVED")

    # ---- stop handling, watchdog, flatten, cancel ---------------------------------------------

    def _on_live_event(self, kind, d):
        """Sync hook from LiveState. Schedules the async handler on the running loop."""
        if kind == "position":
            try:
                asyncio.get_running_loop().create_task(self.handle_position(d))
            except RuntimeError:
                pass

    @staticmethod
    def _pos_side(p):
        # PositionType: 1 = Long (documented). 2 = Short is UNVERIFIED until a sim fill confirms it.
        return {1: "long", 2: "short"}.get(p.get("type"))

    def _close_intents(self, t: Ticket) -> None:
        """The position this ticket owned is gone: its intents no longer describe live exposure."""
        for tag in (t.custom_tag, f"px-{t.hash8}-2"):
            if tag in self._intents and self._intents[tag].get("state") in ("ACKED", "FOUND"):
                try:
                    self._set_intent(tag, state="CLOSED")
                except OSError:
                    pass

    async def handle_position(self, p: dict) -> None:
        """React to a position event: protect a fresh fill, resize the stop, or close out the ticket."""
        cid, size = p.get("contractId"), abs(p.get("size") or 0)
        active = [t for t in self.tickets.values() if t.contract_id == cid and t.state in (PLACING, PLACED, UNKNOWN)]
        recent_rej = [t for t in self.tickets.values() if t.contract_id == cid and t.state == REJECTED
                      and t.broker_order_id is not None and self.clock() - t.rejected_at < REJECTED_LIVE_S]
        newest_rej = max(recent_rej, key=lambda x: x.rejected_at) if recent_rej else None    # one owner, never two stops
        for t in list(self.tickets.values()):
            if t.contract_id != cid:
                continue
            # A fill can land while place() is still awaiting the POST reply (PLACING), after an ambiguous reply
            # (UNKNOWN), or after a rejection that carried an order id (the docs say the order may still exist).
            # A real position must be protected in every one of those states.
            rejected_live = t is newest_rej and not active
            if t.state not in (PLACING, PLACED, UNKNOWN) and not rejected_live:
                continue
            if size == 0:
                if t.filled_size and t.state == PLACED:
                    t.state = "CLOSED"
                    self._cancel_watchdog(t)
                    self._close_intents(t)
                    if t.stop_state == "SENDING":
                        t.abandoned = True      # the stop request is still in flight: its ACK must be cancelled, not trusted
                    elif t.stop_state == "ACKED" and t.stop_order_id is not None:
                        await self._retire_stop_after_close(t)
                continue
            if self._pos_side(p) != t.side:
                # A nonzero position in our contract whose side is not the ticket's (or not a side we recognise: the Short enum is
                # unverified) is never benign: either the enum is wrong or the position was flipped. Silently skipping it left a
                # fill with no stop and no watchdog while protect_open_positions reported it as handled. Flatten and halt.
                await self._naked(t, "position_side_unknown")
                continue
            if rejected_live and not t.filled_size:
                self.trip_halt("rejected_but_filled")       # protect it, then a human looks at why a rejection filled
            t.filled_size = size
            if t.position_id is None and isinstance(p.get("id"), int) and not isinstance(p.get("id"), bool):
                t.position_id = p["id"]
            if t.stop_plan["mode"] == "separate_after_fill":
                if t.stop_state is None:
                    await self._arm_stop(t, size)
                elif t.stop_state == "ACKED" and size != t.stop_size:
                    await self._resize_stop(t, size)

    async def protect_open_positions(self) -> dict:
        """Run after boot and after every resync: every open position must be a protected one.

        The hub can drop or the process restart between an entry ACK and its fill; a REST re-seed records the
        position but raises no event, so without this the position sits naked. For each open position:
          known ticket       -> handle_position (arms or resizes its stop)
          broker stop covers -> fine
          our own intent     -> flatten and halt (we placed it and cannot prove it has a stop)
          anything else      -> an external position (placed by hand); reported, never touched
        """
        out = {}
        if not (self.live.positions_seeded and self.live.account_id is not None):
            return out
        positions = [p for p in list(self.live.positions.values()) if abs(p.get("size") or 0) and p.get("contractId")]
        held = {p["contractId"] for p in positions}
        for tag, rec in list(self._intents.items()):        # entries whose exposure is gone are no longer ours
            if tag.endswith("-1") and rec.get("state") in ("ACKED", "FOUND") and rec.get("contract") not in held \
                    and rec.get("order_id") not in self.live.orders:
                try:
                    self._set_intent(tag, state="CLOSED")
                except OSError:
                    pass
        try:
            working = await self.client.open_orders(self.live.account_id)
        except Exception:  # noqa: BLE001 - cannot prove coverage
            working = None
        for p in positions:
            cid, size = p["contractId"], abs(p["size"])
            side = self._pos_side(p)
            owner = next((t for t in self.tickets.values() if t.contract_id == cid
                          and t.state in (PLACING, PLACED, UNKNOWN)), None)
            if owner is not None and owner.stop_state == "ACKED" and working is not None \
                    and owner.stop_order_id not in {o.get("id") for o in working}:
                # The ticket thinks it has a stop, but the broker book does not list it. What that means is decided by
                # EVIDENCE, in this order, and never by the broker position id alone (whether it is stable is unverified):
                #   1. another stop already covers the position (the trader replaced ours): protected, leave it alone.
                #   2. our stop FILLED (a fill with its order id): the position we owned is gone, so this is a new,
                #      external one: retire the ticket and judge it afresh, never touch it.
                #   3. neither: the stop was cancelled or lost and this is still OUR position, now naked: flatten and halt.
                if self._covered_by_broker_stop(working, cid, side, size):
                    self._audit("stop", ticket=owner.hash8, state="REPLACED_BY_COVERING_STOP")
                    owner.state = "CLOSED"
                    self._cancel_watchdog(owner)
                    self._close_intents(owner)
                    out[cid] = "COVERED"
                    continue
                if not self._stop_filled(owner):
                    self._audit("halt", code="owned_position_stop_missing")
                    res = None
                    try:
                        res = await self.flatten(contract_id=cid, origin="ui")
                    finally:
                        if not (res is not None and res.flat and not res.closed):   # already gone (the stop filled mid-check): no halt
                            self.trip_halt("owned_position_stop_missing")
                    out[cid] = "FLATTENED"
                    continue
                owner.state = "CLOSED"           # our stop filled: the position we owned is gone; judge this one afresh
                self._cancel_watchdog(owner)
                self._close_intents(owner)
                owner = None
            if owner is not None:
                await self.handle_position(p)
                if owner.abandoned:                                       # handle_position already flattened and halted
                    out[cid] = "FLATTENED"
                    continue
                if owner.stop_plan["mode"] == "separate_after_fill" and owner.stop_state not in ("ACKED", "SENDING"):
                    await self._naked(owner, "owned_position_not_protected")      # handled is not protected: prove it
                    out[cid] = "FLATTENED"
                    continue
                out[cid] = "TICKET"
                continue
            covered = self._covered_by_broker_stop(working, cid, side, size)
            if covered:
                out[cid] = "COVERED"
                continue
            mine = any(rec.get("state") in ("ACKED", "FOUND", "SENDING", "UNKNOWN") and tag.endswith("-1")
                       and rec.get("contract") == cid for tag, rec in self._intents.items())
            if mine:
                self._audit("halt", code="unprotected_position")
                try:
                    await self.flatten(contract_id=cid, origin="ui")
                finally:
                    self.trip_halt("unprotected_position")
                out[cid] = "FLATTENED"
            else:
                out[cid] = "EXTERNAL_UNPROTECTED"
        self.protection = out
        return out

    @staticmethod
    def _covered_by_broker_stop(working, cid, side, size) -> bool:
        return working is not None and side is not None and any(
            o.get("contractId") == cid and o.get("type") == 4 and o.get("side") == (1 if side == "long" else 0)
            and abs(o.get("size") or 0) >= size for o in working)

    def _stop_filled(self, t) -> bool:
        """Did THIS ticket's stop fill? Proved only by a non-voided fill carrying its order id (the live trade list)."""
        oid = t.stop_order_id
        return oid is not None and any(tr.get("orderId") == oid and not tr.get("voided") for tr in self.live.trades)

    def _cancel_watchdog(self, t):
        w = self._watchdogs.pop(t.ticket_id, None)
        if w and not w.done() and w is not asyncio.current_task():
            w.cancel()

    async def _arm_stop(self, t: Ticket, size: int) -> None:
        t.stop_state = "SENDING"
        self._watchdogs[t.ticket_id] = asyncio.create_task(self._watchdog(t))
        tag = f"px-{t.hash8}-2"
        body = {"accountId": self.live.account_id, "contractId": t.contract_id, "type": 4,
                "side": t.stop_plan["side"], "size": size, "limitPrice": None,
                "stopPrice": t.stop_plan["stop_price"], "trailPrice": None, "customTag": tag}
        ok = late = False
        try:   # no global lock: a hung stop write must not be able to block the watchdog's flatten
            self._set_intent(tag, state="SENDING", hash8=t.hash8, root=t.root, size=size,
                             ts=self.wall().isoformat(timespec="seconds"))
            self._audit("stop", ticket=t.hash8, root=t.root, size=size, state="SENDING")
            resp = await self.client.write(ORDER_PLACE, body)
            oid = resp.get("orderId") if isinstance(resp, dict) else None
            if isinstance(resp, dict) and resp.get("success") is True and isinstance(oid, int):
                if t.abandoned:       # the watchdog already flattened and halted; this stop is now an orphan
                    late = True
                    t.stop_state = "ABANDONED"
                    self._set_intent(tag, state="ORPHAN", order_id=oid)
                    await self._cancel_late_stop(t, oid, clean_ok=(t.state == "CLOSED"))
                else:
                    t.stop_state, t.stop_order_id, t.stop_size, ok = "ACKED", oid, size, True
                    t.stop_current = t.stop_plan["stop_price"]
                    self._set_intent(tag, state="ACKED", order_id=oid)
                    self._audit("stop", ticket=t.hash8, state="ACKED")
            else:
                t.stop_state = "FAILED"
                self._set_intent(tag, state="UNKNOWN")
                self._audit("stop", ticket=t.hash8, state="FAILED")
        except asyncio.CancelledError:
            t.stop_state = "FAILED"
            raise
        except Exception:  # noqa: BLE001 - any failure leaves the position unprotected
            t.stop_state = "FAILED"
            self._set_intent(tag, state="UNKNOWN")
            self._audit("stop", ticket=t.hash8, state="FAILED")
        if late:
            return
        if ok:
            self._cancel_watchdog(t)
            now = sum(abs(q.get("size") or 0) for q in self.live.positions.values()
                      if q.get("contractId") == t.contract_id)
            if now and now != t.stop_size:      # the position grew (partial fills) while the stop was in flight
                await self._resize_stop(t, now)
        else:
            await self._naked(t, "stop_failed")

    async def _stop_working(self, order_id) -> Optional[bool]:
        """Is this order still in the BROKER's working book? None = could not tell (after retries)."""
        for _ in range(max(1, self.verify_tries)):
            try:
                return any(o.get("id") == order_id for o in await self.client.open_orders(self.live.account_id))
            except Exception:  # noqa: BLE001 - a transient read failure must not halt a normal exit: retry first
                await self.sleep(self.verify_interval)
        return None

    async def _retire_stop_after_close(self, t: Ticket) -> None:
        """The position closed by some means other than our flatten. A standalone stop that is STILL WORKING would open the
        opposite position when hit. A stop that just FILLED (the normal exit) is gone, and its fill proves it, so no
        broker call and no halt. Otherwise prove the book is clear; clean up and carry on only if the cleanup is confirmed."""
        oid = t.stop_order_id
        if self._stop_filled(t) or oid not in self.live.orders:
            return
        for _ in range(max(1, self.verify_tries)):          # a just-filled stop can linger in the listing for a beat
            working = await self._stop_working(oid)
            if working is False:
                return
            if working is None:
                break
            await self.sleep(self.verify_interval)
            if self._stop_filled(t) or oid not in self.live.orders:
                return
        else:
            working = True
        if working is None:
            self._audit("stop", ticket=t.hash8, state="CLOSE_UNVERIFIED")
            self.trip_halt("stop_unverified_after_close")
            return
        cancelled = False
        if self.limits.account_kind == "sim":
            try:
                r = await self.client.write(ORDER_CANCEL, {"accountId": self.live.account_id, "orderId": oid})
                cancelled = isinstance(r, dict) and r.get("success") is True
            except Exception:  # noqa: BLE001
                cancelled = False
        if cancelled and await self._stop_working(oid) is False:
            self._audit("stop", ticket=t.hash8, state="CLOSE_CLEANED")     # the book confirms it: no halt for a manual close
            return
        try:                                                   # keep the evidence: a leftover stop is an orphan, not "closed"
            self._set_intent(f"px-{t.hash8}-2", state="ORPHAN", order_id=oid)
        except OSError:
            pass
        self._audit("stop", ticket=t.hash8, state="LATE_ORPHAN")
        self.trip_halt("stop_orphan_after_close")

    async def _cancel_late_stop(self, t: Ticket, order_id: int, clean_ok: bool = False) -> None:
        """A stop ACKed after its position was flattened or closed. Left alone it can open the opposite position.
        `clean_ok` (an ordinary close, not the watchdog) lets a confirmed cleanup finish without a halt."""
        cancelled = False
        if self.limits.account_kind == "sim":
            try:
                r = await self.client.write(ORDER_CANCEL, {"accountId": self.live.account_id, "orderId": order_id})
                cancelled = isinstance(r, dict) and r.get("success") is True
            except Exception:  # noqa: BLE001
                cancelled = False
        working = await self._stop_working(order_id)       # replies are not proof; the broker book is
        gone = working is False
        no_position = not any(abs(q.get("size") or 0) for q in self.live.positions.values() if q.get("contractId") == t.contract_id)
        if gone and (cancelled or no_position):
            self._audit("stop", ticket=t.hash8, state="LATE_CANCELLED" if cancelled else "LATE_GONE")
            if not clean_ok:
                self.trip_halt("late_stop_cancelled" if cancelled else "late_stop_gone")
            return
        try:
            self._set_intent(f"px-{t.hash8}-2", state="ORPHAN", order_id=order_id)
        except OSError:
            pass
        self._audit("stop", ticket=t.hash8, state="LATE_ORPHAN")
        self.trip_halt("late_stop_orphan")

    async def _resize_stop(self, t: Ticket, size: int) -> None:
        """Keep the stop equal to the position, AT ITS CURRENT PRICE. A stop larger than the position flips you on a
        stop-out, and re-sending the original price would undo a tightening the trader already made."""
        failed = True
        async with self._stop_lock:
            price = t.stop_current if t.stop_current is not None else t.stop_plan["stop_price"]
            try:
                resp = await self.client.write(ORDER_MODIFY, {"accountId": self.live.account_id,
                                                              "orderId": t.stop_order_id, "size": size,
                                                              "limitPrice": None, "stopPrice": price,
                                                              "trailPrice": None})
                if isinstance(resp, dict) and resp.get("success") is True:
                    t.stop_size = size
                    self._audit("stop", ticket=t.hash8, size=size, state="RESIZED")
                    failed = False
            except Exception:  # noqa: BLE001
                pass
        if failed:
            await self._naked(t, "stop_resize_failed")

    async def _watchdog(self, t: Ticket) -> None:
        await self.sleep(self.limits.stop_ack_s)
        # Armed only once exposure exists, so it must not depend on the entry's reply state (PLACING, UNKNOWN, rejected).
        if t.stop_state != "ACKED" and not t.abandoned and t.state != "CLOSED":
            await self._naked(t, "stop_timeout")

    async def _naked(self, t: Ticket, code: str) -> None:
        """A position without a confirmed stop: flatten, then freeze everything for a human."""
        t.abandoned = True
        self._audit("halt", ticket=t.hash8, code=code)
        try:
            await self.flatten(contract_id=t.contract_id, origin="ui")
        finally:
            self.trip_halt("naked_position")

    async def _positions_rest(self, contract_id):
        pos = await self.client.open_positions(self.live.account_id)
        return [p for p in pos if contract_id is None or p.get("contractId") == contract_id]

    async def _working_rest(self, contract_id):
        ords = await self.client.open_orders(self.live.account_id)
        return [o for o in ords if contract_id is None or o.get("contractId") == contract_id]

    async def _verify_flat(self, contract_id) -> bool:
        for _ in range(max(1, self.verify_tries)):
            try:
                if not await self._positions_rest(contract_id):
                    return True
            except Exception:  # noqa: BLE001 - cannot confirm; keep trying, then report not flat
                pass
            await self.sleep(self.verify_interval)
        return False

    async def flatten(self, *, contract_id: Optional[str] = None, origin: str = "ui") -> FlattenResult:
        """Close position(s) and, on a simulated account, cancel working orders. Always allowed: ignores PX_TRADING
        and the halt file (it only reduces exposure).

        Order: (sim) cancel working orders on the contract EVEN WITH NO POSITION (a resting entry can fill later) and
        verify they are gone; closeContract; verify flat by REST re-reads. If still open the default is to halt and say
        so: a timed-out close can still execute, so an automatic opposite order could flip the position. That
        fallback (capped at the net position read just before sending) exists only behind PX_FLATTEN_FALLBACK=1.
        Live accounts cannot cancel via API, so leftover working orders are reported as orphans.
        """
        async with self._flatten_lock:
            reasons, closed = [], []
            try:
                targets = await self._positions_rest(contract_id)
            except Exception:  # noqa: BLE001 - fall back to the desk's own view rather than do nothing
                targets = [p for p in self.live.positions.values()
                           if contract_id is None or p.get("contractId") == contract_id]
                reasons.append("positions read from desk view: REST unavailable")
            sim = self.limits.account_kind == "sim"
            orphans, unread = 0, False
            if sim:
                try:
                    for o in await self._working_rest(contract_id):
                        ok = False
                        try:
                            r = await self.client.write(ORDER_CANCEL, {"accountId": self.live.account_id, "orderId": o["id"]})
                            ok = isinstance(r, dict) and r.get("success") is True
                        except Exception:  # noqa: BLE001
                            ok = False
                        if not ok:
                            orphans += 1
                            reasons.append("a working order could not be cancelled")
                    if not orphans:
                        left = await self._working_rest(contract_id)      # cancel replies are not proof; read the book
                        if left:
                            orphans = len(left)
                            reasons.append("working orders remain after cancel")
                except Exception:  # noqa: BLE001
                    unread = True
                    reasons.append("working orders could not be read")
            verified_empty = False
            try:     # a resting entry can fill while we cancel: the position list read BEFORE the cancels is not proof of flat
                targets = await self._positions_rest(contract_id)
                verified_empty = not targets
            except Exception:  # noqa: BLE001
                reasons.append("positions could not be re-read after cancelling orders")
            for p in targets:
                try:
                    await self.client.write(POSITION_CLOSE, {"accountId": self.live.account_id,
                                                             "contractId": p["contractId"]})
                    closed.append("close")
                except Exception:  # noqa: BLE001 - outcome checked by re-read below
                    reasons.append("closeContract outcome unclear")
            flat = await self._verify_flat(contract_id) if targets else verified_empty
            if targets and not flat:
                if self.limits.flatten_fallback:
                    try:
                        for p in await self._positions_rest(contract_id):          # fresh read, then cap at the net
                            net = abs(p.get("size") or 0)
                            side = self._pos_side(p)
                            if net < 1 or side is None:
                                continue
                            await self.client.write(ORDER_PLACE, {
                                "accountId": self.live.account_id, "contractId": p["contractId"], "type": 2,
                                "side": 1 if side == "long" else 0, "size": net, "limitPrice": None,
                                "stopPrice": None, "trailPrice": None,
                                "customTag": f"px-{uuid.uuid4().hex[:8]}-9"})
                            closed.append("market")
                    except Exception:  # noqa: BLE001
                        reasons.append("opposite market order outcome unclear")
                    flat = await self._verify_flat(contract_id)
                else:
                    reasons.append("closeContract not confirmed and the automatic fallback is off: close it in the TopstepX UI")
            if sim and (orphans or unread):
                flat = False                    # a working order can still fill: not flat
            if not sim:
                try:
                    orphans = len(await self._working_rest(contract_id))
                except Exception:  # noqa: BLE001
                    orphans = -1
                if orphans:
                    reasons.append("working orders remain: cancel them in the TopstepX UI (API cannot on live)")
            self._audit("flatten", origin=origin, state="FLAT" if flat else "NOT_FLAT", size=len(closed))
            if not flat:
                self.trip_halt("flatten_failed")
                reasons.append("exposure not confirmed clear: halted")
            return FlattenResult(flat, tuple(closed), orphans, tuple(reasons))

    async def cancel(self, order_id: int, *, origin: str = "ui") -> bool:
        """Cancel a working order. The API supports this on simulated accounts only."""
        if self.limits.account_kind != "sim":
            raise GuardError("the API cannot cancel orders on a live account; cancel it in the TopstepX UI")
        if not isinstance(order_id, int) or isinstance(order_id, bool):
            raise GuardError("order_id must be an integer")
        async with self._lock:
            resp = await self.client.write(ORDER_CANCEL, {"accountId": self.live.account_id, "orderId": order_id})
        ok = isinstance(resp, dict) and resp.get("success") is True
        self._audit("cancel", origin=origin, state="OK" if ok else "FAILED")
        return ok

    # ---- stop management: tighten only ---------------------------------------------------------

    def open_trade(self, contract_id: str):
        """The ticket that owns the open position in this contract, if its stop is live."""
        cands = [t for t in self.tickets.values() if t.contract_id == contract_id and t.state == PLACED
                 and t.filled_size and t.stop_state == "ACKED" and t.stop_order_id]
        return max(cands, key=lambda t: t.created) if cands else None

    async def move_stop(self, contract_id: str, new_price, *, origin: str = "ui") -> MoveResult:
        """Move the working stop TOWARD price. Never wider, never into the market, never above the position.

        Reduce-only by construction: a long's stop may only rise, a short's only fall, so this can lower the
        loss you are exposed to and cannot add exposure. It is allowed while halted or with PX_TRADING=0.
        Modify on a LIVE account is undocumented (cancel is sim-only), so it is refused there.
        """
        lim = self.limits
        if lim.account_kind != "sim":
            return MoveResult(False, None, "stop modify on a live account is undocumented: move it in the TopstepX UI")
        if not _finite_pos(new_price):
            return MoveResult(False, None, "stop price invalid")
        async with self._stop_lock:
            t = self.open_trade(contract_id)
            if t is None:
                return MoveResult(False, None, "no open position with a live stop in that contract")
            try:
                rec = await self._contract(contract_id)
                tick = Decimal(str(rec["tickSize"]))
            except Exception:  # noqa: BLE001
                return MoveResult(False, None, "contract record unavailable")
            new, cur = Decimal(str(new_price)), Decimal(str(t.stop_current))
            if (new / tick) % 1 != 0:
                return MoveResult(False, None, "stop is not on a tick")
            long_ = t.side == "long"
            if (long_ and new <= cur) or (not long_ and new >= cur):
                return MoveResult(False, None, "a stop only moves toward price: it can never be widened or left where it is")
            q = self.live.quotes.get(contract_id) or {}
            if self.clock() - q.get("_rx", -1e9) > lim.max_quote_age_s:
                return MoveResult(False, None, "quote is stale: cannot check the stop is clear of the market")
            edge = q.get("bestBid") if long_ else q.get("bestAsk")
            if not _finite_pos(edge):
                return MoveResult(False, None, "no usable quote")
            if (long_ and new > Decimal(str(edge)) - tick) or (not long_ and new < Decimal(str(edge)) + tick):
                return MoveResult(False, None, "stop would sit at or through the market: it would fill immediately")
            body = {"accountId": self.live.account_id, "orderId": t.stop_order_id, "size": t.stop_size,
                    "limitPrice": None, "stopPrice": float(new), "trailPrice": None}
            self._audit("stop", ticket=t.hash8, state="MOVING")
            try:
                resp = await self.client.write(ORDER_MODIFY, body)
            except PXError as e:
                if not isinstance(e, (PXWriteUnknown, PXRateLimited)):
                    return MoveResult(False, None, "not sent")
                return await self._verify_stop_after_unknown(t, float(new))
            if isinstance(resp, dict) and resp.get("success") is True:
                t.stop_current = float(new)
                self._audit("stop", ticket=t.hash8, state="MOVED")
                return MoveResult(True, float(new), "")
            return MoveResult(False, None, "broker did not accept the modify; the old stop is unchanged")

    async def _verify_stop_after_unknown(self, t: Ticket, wanted: float) -> MoveResult:
        """A modify with no clear reply: read the broker. Whatever it says, a stop must still exist."""
        try:
            orders = await self.client.open_orders(self.live.account_id)
            mine = next((o for o in orders if o.get("id") == t.stop_order_id), None)
        except Exception:  # noqa: BLE001
            mine = None
            orders = None
        if mine is not None and isinstance(mine.get("stopPrice"), (int, float)):
            t.stop_current = float(mine["stopPrice"])
            ok = abs(t.stop_current - wanted) < 1e-9
            self._audit("stop", ticket=t.hash8, state="MOVED" if ok else "UNCHANGED")
            return MoveResult(ok, t.stop_current, "" if ok else "modify did not apply; the old stop is still working")
        await self._naked(t, "stop_lost_after_modify")      # cannot confirm any stop exists: flatten and halt
        return MoveResult(False, None, "could not confirm the stop after the modify: flattened and halted")
