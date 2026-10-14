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

import sizing
from px_client import PXError, PXRateLimited, PXWriteUnknown
from px_limits import BRACKET_MODES, DayState, Limits, atomic_write_json
from px_time import CT

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
        if t >= SESSION_OPEN or t < cutoff:
            return None
        return f"no new entries after {cut} CT"

    # ---- shared budget and stop helpers (one definition, used by the UI, MCP and digest) -----------

    def budget(self, now=None):
        """(remaining, daily_room) in dollars, or (None, None) if P&L or limits are unknown. Fails closed."""
        lim = self.limits
        pnl = self.live.day_pnl(now or self.wall())
        if pnl is None or lim.daily_loss_limit <= 0 or lim.dd_buffer <= 0:
            return None, None
        room = lim.daily_loss_limit + pnl
        return min(room, lim.dd_buffer), room

    def risk_dollars_for(self, risk_pct: float, now=None) -> float:
        """risk_pct of the REMAINING budget (min of daily room and drawdown buffer), never above either."""
        remaining, room = self.budget(now)
        if remaining is None or remaining <= 0:
            return 0.0
        return sizing.risk_budget(remaining, risk_pct, room, self.limits.dd_buffer)

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
            ticks_dec = abs(Decimal(str(entry_px)) - Decimal(str(stop_price))) / Decimal(str(spec.tick_size))
            aligned = (Decimal(str(stop_price)) / Decimal(str(spec.tick_size))) % 1 == 0
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

        pnl = self.live.day_pnl(now)
        chk("day_pnl_known", pnl is not None, "today's P&L is unknown (trades not read from REST)")
        chk("limits_configured", lim.daily_loss_limit > 0 and lim.dd_buffer > 0,
            "PX_DAILY_LOSS_LIMIT and PX_DRAWDOWN_BUFFER must be set")
        remaining = None
        if pnl is not None and lim.daily_loss_limit > 0 and lim.dd_buffer > 0:
            remaining = min(lim.daily_loss_limit + pnl, lim.dd_buffer)
            chk("budget_remaining", remaining > 0, "no loss room left today")
        chk("orders_today", self.day.orders_today(now) < lim.max_orders_day, "daily order count reached")
        b = self.behavior(now)
        chk("cooldown_after_loss", b["cooldown_left_s"] == 0,
            f"cooldown after a loss: {b['cooldown_left_s'] // 60}m{b['cooldown_left_s'] % 60:02d}s left. Walk away.")
        chk("loss_lockout", not b["locked"], f"locked for the session: {b['consecutive_losses']} losses in a row")
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
                t.state = REJECTED          # a refused place consumes the ticket; restage against the new world
                return self._deny(t, reasons, origin, REJECTED)

            # ---- commit point: from here a crash or timeout means UNKNOWN, never "probably not sent"
            try:
                self.day.count_order(self.wall(), risk=t.readback.get("risk_usd"))
                self._set_intent(t.custom_tag, state="SENDING", hash8=t.hash8, root=t.root, size=t.payload["size"],
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
                t.state, t.acked = PLACED, True
                self._set_intent(t.custom_tag, state="ACKED", order_id=oid)
                self._audit("ack", ticket=t.hash8, state=PLACED, code=str(code))
                return PlaceResult(True, PLACED, order_id=oid, acked=True, error_code=code)
            if resp["success"] is False:       # definitive refusal; the docs say the order may still exist (orderId kept)
                t.state = REJECTED
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

    def resolve_intent(self, tag: str, note: str = "resolved") -> None:
        """Human action: mark an unreconciled intent as dealt with (e.g. after checking TopstepX)."""
        if tag in self._intents:
            self._set_intent(tag, state="RESOLVED")
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

    async def handle_position(self, p: dict) -> None:
        """React to a position event: protect a fresh fill, resize the stop, or close out the ticket."""
        cid, size = p.get("contractId"), abs(p.get("size") or 0)
        for t in list(self.tickets.values()):
            # A fill can land while place() is still awaiting the POST reply (PLACING), or after an
            # ambiguous reply (UNKNOWN). A real position must be protected in every one of those states.
            if t.contract_id != cid or t.state not in (PLACING, PLACED, UNKNOWN):
                continue
            if size == 0:
                if t.filled_size and t.state == PLACED:
                    t.state = "CLOSED"
                    self._cancel_watchdog(t)
                continue
            if self._pos_side(p) != t.side:
                continue
            t.filled_size = size
            if t.stop_plan["mode"] == "separate_after_fill":
                if t.stop_state is None:
                    await self._arm_stop(t, size)
                elif t.stop_state == "ACKED" and size != t.stop_size:
                    await self._resize_stop(t, size)

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
        ok = False
        try:   # no global lock: a hung stop write must not be able to block the watchdog's flatten
            self._set_intent(tag, state="SENDING", hash8=t.hash8, root=t.root, size=size,
                             ts=self.wall().isoformat(timespec="seconds"))
            self._audit("stop", ticket=t.hash8, root=t.root, size=size, state="SENDING")
            resp = await self.client.write(ORDER_PLACE, body)
            oid = resp.get("orderId") if isinstance(resp, dict) else None
            if isinstance(resp, dict) and resp.get("success") is True and isinstance(oid, int):
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
        if ok:
            self._cancel_watchdog(t)
            now = sum(abs(q.get("size") or 0) for q in self.live.positions.values()
                      if q.get("contractId") == t.contract_id)
            if now and now != t.stop_size:      # the position grew (partial fills) while the stop was in flight
                await self._resize_stop(t, now)
        else:
            await self._naked(t, "stop_failed")

    async def _resize_stop(self, t: Ticket, size: int) -> None:
        """Keep the stop equal to the position. A stop larger than the position flips you on a stop-out."""
        try:
            resp = await self.client.write(ORDER_MODIFY, {"accountId": self.live.account_id,
                                                          "orderId": t.stop_order_id, "size": size,
                                                          "limitPrice": None, "stopPrice": t.stop_plan["stop_price"],
                                                          "trailPrice": None})
            if isinstance(resp, dict) and resp.get("success") is True:
                t.stop_size = size
                self._audit("stop", ticket=t.hash8, size=size, state="RESIZED")
                return
        except Exception:  # noqa: BLE001
            pass
        await self._naked(t, "stop_resize_failed")

    async def _watchdog(self, t: Ticket) -> None:
        await self.sleep(self.limits.stop_ack_s)
        if t.stop_state != "ACKED" and t.state == PLACED:
            await self._naked(t, "stop_timeout")

    async def _naked(self, t: Ticket, code: str) -> None:
        """A position without a confirmed stop: flatten, then freeze everything for a human."""
        self._audit("halt", ticket=t.hash8, code=code)
        try:
            await self.flatten(contract_id=t.contract_id, origin="ui")
        finally:
            self.trip_halt("naked_position")

    async def _positions_rest(self, contract_id):
        pos = await self.client.open_positions(self.live.account_id)
        return [p for p in pos if contract_id is None or p.get("contractId") == contract_id]

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
        """Close position(s). Always allowed: ignores PX_TRADING and the halt file (it only reduces exposure).

        Order: (sim only) cancel working orders; closeContract; verify flat by REST re-reads; if still
        open, an opposite market order capped at the net position read immediately before sending.
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
            if not targets:
                return FlattenResult(True, (), 0, tuple(reasons))
            sim = self.limits.account_kind == "sim"
            if sim:
                try:
                    for o in await self.client.open_orders(self.live.account_id):
                        if contract_id is None or o.get("contractId") == contract_id:
                            try:
                                await self.client.write(ORDER_CANCEL, {"accountId": self.live.account_id,
                                                                       "orderId": o["id"]})
                            except Exception:  # noqa: BLE001
                                reasons.append("a working order could not be cancelled")
                except Exception:  # noqa: BLE001
                    reasons.append("working orders could not be read")
            for p in targets:
                try:
                    await self.client.write(POSITION_CLOSE, {"accountId": self.live.account_id,
                                                             "contractId": p["contractId"]})
                    closed.append("close")
                except Exception:  # noqa: BLE001 - outcome checked by re-read below
                    reasons.append("closeContract outcome unclear")
            flat = await self._verify_flat(contract_id)
            if not flat:
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
            orphans = 0
            if not sim:
                try:
                    orphans = len([o for o in await self.client.open_orders(self.live.account_id)
                                   if contract_id is None or o.get("contractId") == contract_id])
                except Exception:  # noqa: BLE001
                    orphans = -1
                if orphans:
                    reasons.append("working orders remain: cancel them in the TopstepX UI (API cannot on live)")
            self._audit("flatten", origin=origin, state="FLAT" if flat else "NOT_FLAT", size=len(closed))
            if not flat:
                self.trip_halt("flatten_failed")
                reasons.append("position not confirmed flat: halted")
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
