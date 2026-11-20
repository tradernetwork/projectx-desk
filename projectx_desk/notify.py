"""Push notifier: watches the desk's own read-only state and sends short fixed-template phone notifications.

READ-ONLY by construction. It gets three injected callables from app.py (the /ai/state dict, a few read-only extras, a contract
chooser) and a Pusher. It imports nothing from the guard, the client or the limits, and has no way to place, cancel, flatten,
halt or clear anything. A notification is text that goes OUT; nothing a phone sends back is read here.

Payload text is built only from enums and fixed templates. The only variable parts are a contract ROOT that passes a strict
pattern (MES, MNQ, ...), BUY/SELL, LONG/SHORT, and, when PX_PUSH_AMOUNTS=1, the net P&L of the closing fills (never a balance,
never an account id, order tag, contract name or message text). A hostile name falls back to the word "contract".

Config (env): PX_PUSH_AMOUNTS (default 0), PX_PUSH_GRACE_S (default 10), PX_PUSH_INTERVAL_S (default 5, min 2).
Caps: HOURLY_CAP overall and PER_CONTRACT_CAP per contract per rolling hour; what a cap holds back is logged (`Notifier.log`) and later
sent as ONE "N alerts held back" summary. `LevelWatch` is the pure M17 level-cross rule set (not yet wired to a level source).
Events (each a per-subscription toggle): halt, flat, fill, stop, room, feed (default ON); callout, regime (default OFF).
"""
import asyncio
import os
import re
import time
from collections import deque
from typing import NamedTuple

TITLE = "ProjectX Desk"
DEBOUNCE_S = 60.0
HOURLY_CAP = 20
PER_CONTRACT_CAP = 5
MAX_QUOTE_AGE_S = 20.0
FEED_DOWN_S = 30.0
ROOM_LOW, ROOM_REARM = 0.25, 0.35
CALLOUT_FRESH_S = 300
RETRY_WINDOW_S = 60.0                  # a fill or critical alert that failed transiently is retried only this long after the event
RETRY_GAP_S = 10.0                     # and no sooner than this after the previous attempt
RETRY_MAX_ATTEMPTS = 3                 # per (event, subscription), the first send included
FILL_TTL_S = 60                        # a fill that cannot be delivered within a minute is stale; critical events keep the default TTL
STOP_ORDER_TYPES = (3, 4, 5)           # STOPLIMIT, STOP, TRAIL
_ROOT_RE = re.compile(r"^[A-Z0-9]{1,5}$")
_CID_RE = re.compile(r"^CON\.[A-Za-z0-9._]{1,40}$")
_ENUM = {"SIDE": ("BUY", "SELL"), "DIR": ("LONG", "SHORT")}
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,5}$")


class Event(NamedTuple):
    kind: str
    key: str            # dedupe key, also the notification tag
    text: str
    critical: bool = False     # ignores quiet hours (halt, stop failure, unprotected position)


def root_of(cid):
    """Contract root from a contract id like CON.F.US.MES.Z26 -> MES; anything that does not look right -> None."""
    if not isinstance(cid, str) or not _CID_RE.match(cid):
        return None
    parts = cid.split(".")
    return parts[3] if len(parts) >= 5 and _ROOT_RE.match(parts[3]) else None


def _word(cid):
    return root_of(cid) or "contract"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf") else None


def _contract_of(key):
    """The contract root inside an event key such as stop:MES or callout:MNQ:LONG, else None (halt, room:daily and feed keys have none)."""
    parts = key.split(":") if isinstance(key, str) else []
    return parts[1] if len(parts) > 1 and _ROOT_RE.match(parts[1]) else None


def money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


class Limiter:
    """One notification per key per DEBOUNCE_S, at most HOURLY_CAP per rolling hour overall and PER_CONTRACT_CAP per contract.

    A notification refused by a CAP (not by the debounce, which only drops a repeat) is counted as held back and never charged to the
    window. When the window frees, `take_held` hands back the count once so the notifier can send ONE summary instead of a flood.
    A timestamp exactly 3600 s old is outside the window."""

    def __init__(self, debounce=DEBOUNCE_S, cap=HOURLY_CAP, contract_cap=PER_CONTRACT_CAP):
        self.debounce, self.cap, self.contract_cap = debounce, cap, contract_cap
        self.last, self.sent, self.by_contract = {}, [], {}
        self.held, self.held_by, self.held_kind = 0, {}, None

    def _prune(self, now):
        self.sent = [t for t in self.sent if now - t < 3600]
        self.by_contract = {c: v for c, v in ((c, [t for t in ts if now - t < 3600]) for c, ts in self.by_contract.items()) if v}

    def allow(self, key, now, contract=None, kind=None):
        self._prune(now)
        if key in self.last and now - self.last[key] < self.debounce:
            return False
        if len(self.sent) >= self.cap or (contract is not None and len(self.by_contract.get(contract, ())) >= self.contract_cap):
            self.held += 1
            self.held_by[contract] = self.held_by.get(contract, 0) + 1
            self.held_kind = kind or self.held_kind
            return False
        self.last[key] = now
        self.sent.append(now)
        if contract is not None:
            self.by_contract.setdefault(contract, []).append(now)
        if len(self.last) > 500:
            self.last = {k: t for k, t in self.last.items() if now - t < self.debounce}
        return True

    def take_held(self, now):
        """The number of held-back alerts once the global window and every affected contract have room again; None otherwise. Resets the count."""
        if not self.held:
            return None
        self._prune(now)
        if len(self.sent) >= self.cap or any(c is not None and len(self.by_contract.get(c, ())) >= self.contract_cap for c in self.held_by):
            return None
        n, self.held, self.held_by = self.held, 0, {}
        self.sent.append(now)                      # the summary itself is one notification
        return n


class LevelWatch:
    """Pure level-cross watcher with the M17 rules. observe(levels {key: price}, px, quote_age_s, now) -> [Event].

    First sight of a level records the side only. A cross fires when price moves to the other side by more than `hyst`; a gap fires once.
    The same key and direction then waits until price has retreated `rearm` beyond the level on the other side. A level that disappears
    snoozes with exactly one "Level gone" event and comes back as a fresh silent baseline. A quote older than MAX_QUOTE_AGE_S (or unknown,
    or a non-finite price) is skipped and changes nothing. Text is a closed template; the key never reaches it. Says what happened, never what to do."""

    def __init__(self, hyst=0.0, rearm=0.0):
        self.hyst, self.rearm = hyst, rearm
        self.sides, self.armed, self.gone = {}, {}, set()

    def observe(self, levels, px, quote_age_s, now):
        out = []
        age = _num(quote_age_s)
        if _num(px) is None or age is None or age > MAX_QUOTE_AGE_S or not isinstance(levels, dict):
            return out
        for key in [k for k in self.sides if k not in levels]:
            del self.sides[key]
            self.armed = {k: v for k, v in self.armed.items() if k[0] != key}
            self.gone.add(key)
            out.append(Event("level", f"level_gone:{key}", "Level gone"))
        for key, lv in levels.items():
            lv = _num(lv)
            if lv is None:
                continue
            self.gone.discard(key)
            side = 1 if px > lv + self.hyst else -1 if px < lv - self.hyst else 0
            if side == 0:
                continue
            prev = self.sides.get(key)
            self.sides[key] = side
            if abs(px - lv) >= self.rearm:
                self.armed[(key, -side)] = True
            if prev is None or prev == side:
                continue
            if self.armed.get((key, side)) is False:
                continue
            self.armed[(key, side)] = False
            out.append(Event("level", f"level:{key}:{'up' if side > 0 else 'down'}", "Level crossed up" if side > 0 else "Level crossed down"))
        return out


class Detector:
    """Diffs successive desk-state dicts into events. Holds all the memory (previous positions, timers, seen ids)."""

    def __init__(self, amounts=False):
        self.amounts = amounts
        self.halt = self.unknown = False
        self.positions = None
        self.stop_ids, self.seen_trades = set(), set()
        self.trades_primed = False
        self.stop_failed = False
        self.unprot = set()
        self.room_low = {"daily": False, "dd": False}
        self.dd_ref = 0.0
        self.down_since = {"market": None, "user": None}
        self.down_told = {"market": False, "user": False}
        self.callouts_seen = set()
        self.regime = None

    def step(self, cur, extras, now):
        out = []
        if not isinstance(cur, dict):
            return out
        extras = extras or {}
        mode, risk, feed = cur.get("mode") or {}, cur.get("risk") or {}, cur.get("feed") or {}
        pos = {}
        for p in cur.get("positions") or []:
            c = p.get("contract") if isinstance(p, dict) else None
            if isinstance(c, str) and _CID_RE.match(c):
                pos[c] = (p.get("side"), _num(p.get("size")))
        # --- halt ---
        h, u = bool(mode.get("halt")), bool(mode.get("unknown_order"))
        if h and not self.halt:
            out.append(Event("halt", "halt", "HALT: new orders refused", True))
        if u and not self.unknown:
            out.append(Event("halt", "unknown_order", "Unreconciled order: check desk", True))
        self.halt, self.unknown = h, u
        # --- fills, stops, flat ---
        stop_now = {o.get("id") for o in (extras.get("orders") or {}).values()
                    if isinstance(o, dict) and o.get("type") in STOP_ORDER_TYPES and o.get("status") in (1, 6)}
        known_stops = self.stop_ids | stop_now
        new_by_cid = {}
        for t in extras.get("trades") or []:
            if not isinstance(t, dict) or t.get("id") is None or t["id"] in self.seen_trades:
                continue
            self.seen_trades.add(t["id"])
            if self.trades_primed:
                cid = t.get("contractId")
                new_by_cid.setdefault(cid if isinstance(cid, str) and _CID_RE.match(cid) else "?", []).append(t)
        if len(self.seen_trades) > 2000:
            self.seen_trades = set(list(self.seen_trades)[-1000:])
        self.trades_primed = True
        flat = [] if self.positions is None else [c for c in self.positions if c not in pos]
        told = set()
        for cid in flat:
            fs = new_by_cid.get(cid, [])
            stop_hit = any(t.get("orderId") in known_stops for t in fs)
            net = sum((_num(t.get("profitAndLoss")) or 0.0) - (_num(t.get("fees")) or 0.0) for t in fs) if fs else None
            tail = f" {money(net)}" if self.amounts and net is not None else ""
            w = _word(cid)
            out.append(Event("stop", f"stop:{w}", f"Stop hit {w}{tail}") if stop_hit else Event("flat", f"flat:{w}", f"Flat {w}{tail}"))
            told.add(cid)
        for cid, fs in new_by_cid.items():
            if cid in told:
                continue
            t = fs[-1]
            side = "BUY" if t.get("side") == 0 else "SELL" if t.get("side") == 1 else None
            w = _word(cid) if cid != "?" else "contract"
            out.append(Event("fill", f"fill:{w}", f"Fill {w}" + (f" {side}" if side else "")))
        self.positions, self.stop_ids = pos, stop_now
        # --- stop failure / unprotected ---
        sf = (cur.get("trade") or {}).get("stop_state") == "FAILED"
        if sf and not self.stop_failed:
            roots = {_word(c) for c in pos}
            out.append(Event("stop", "stop_failed", f"Stop FAILED {roots.pop()}: check desk" if len(roots) == 1 else "Stop FAILED: check desk", True))
        self.stop_failed = sf
        prot = extras.get("protection") or {}
        unp = {c for c, s in prot.items() if s == "EXTERNAL_UNPROTECTED" and c in pos} if isinstance(prot, dict) else set()
        for c in sorted(unp - self.unprot):
            out.append(Event("stop", f"unprotected:{_word(c)}", f"Unprotected position {_word(c)}", True))
        self.unprot = unp
        # --- loss rooms ---
        dl, dr = _num(risk.get("daily_limit")), _num(risk.get("daily_room"))
        if dl and dl > 0 and dr is not None:
            self._room("daily", dr / dl, "Daily room low", out)
        ddr = _num(risk.get("dd_buffer"))
        if ddr is not None:
            self.dd_ref = max(self.dd_ref, ddr, _num(extras.get("dd_limit")) or 0.0)
            if self.dd_ref > 0:
                self._room("dd", ddr / self.dd_ref, "Drawdown room low", out)
        # --- feed ---
        for name, label in (("market", "Market"), ("user", "User")):
            if feed.get(name) == "DOWN":
                if self.down_since[name] is None:
                    self.down_since[name] = now
                if now - self.down_since[name] > FEED_DOWN_S and not self.down_told[name]:
                    self.down_told[name] = True
                    out.append(Event("feed", f"feed_down:{name}", f"{label} feed down"))
            elif feed.get(name) == "UP":
                if self.down_told[name]:
                    out.append(Event("feed", f"feed_up:{name}", f"{label} feed recovered"))
                self.down_since[name], self.down_told[name] = None, False
        # --- analyst entry callout ---
        co = cur.get("callouts") or {}
        for i in (co.get("items") or []) if co.get("status") == "OK" else []:
            if not isinstance(i, dict) or i.get("event") != "ENTRY":
                continue
            k = (i.get("ts"), i.get("symbol"), i.get("side"), i.get("analyst"))
            if k in self.callouts_seen:
                continue
            self.callouts_seen.add(k)
            age = _num(i.get("age_s"))
            if age is not None and age <= CALLOUT_FRESH_S:
                sym = i.get("symbol") if isinstance(i.get("symbol"), str) and _SYMBOL_RE.match(i["symbol"]) else None
                side = i.get("side") if i.get("side") in _ENUM["DIR"] else None
                out.append(Event("callout", f"callout:{sym or 'x'}:{side or 'x'}", "Analyst ENTRY" + (f" {sym}" if sym else "") + (f" {side}" if side else "")))
        if len(self.callouts_seen) > 500:
            self.callouts_seen = set(list(self.callouts_seen)[-250:])
        # --- regime ---
        rg = (cur.get("regime") or {}).get("state")
        if rg in ("TRENDING", "RANGING"):
            if self.regime == "TRENDING" and rg == "RANGING":
                out.append(Event("regime", "regime", "Regime RANGING"))
            self.regime = rg
        return out

    def _room(self, name, frac, text, out):
        if frac < ROOM_LOW and not self.room_low[name]:
            self.room_low[name] = True
            out.append(Event("room", f"room:{name}", text))
        elif frac >= ROOM_REARM:
            self.room_low[name] = False


class Notifier:
    def __init__(self, pusher, state_fn, extras_fn=None, contract_fn=None, amounts=False, grace_s=10.0, limiter=None,
                 clock=time.monotonic):
        self.pusher, self.state_fn, self.extras_fn, self.contract_fn = pusher, state_fn, extras_fn, contract_fn
        self.det, self.limiter, self.grace_s, self.clock = Detector(amounts), limiter or Limiter(), grace_s, clock
        self.armed_at = self.t0 = None
        self.last_regime_poll = -1e9
        self.sent = 0
        self.retries = {}                # event serial -> retry state; separate from the Detector's seen sets on purpose
        self._serial = 0
        self.log = deque(maxlen=500)     # every event that reached the sender stage, pushed or held back

    def wants(self, kind):
        return any(s["prefs"]["events"].get(kind) for s in self.pusher.store.subs.values())

    async def _retry(self, now):
        """Re-send a transient failure (push 'retry': 429, 5xx, network) to ONLY the subscriptions that failed, for fills and critical events,
        at most RETRY_MAX_ATTEMPTS sends per (event, subscription), RETRY_GAP_S apart, within RETRY_WINDOW_S of the event. The limiter is not
        charged again: one event, one slot. A subscription that was pruned is dropped."""
        for serial in list(self.retries):
            r = self.retries[serial]
            if now - r["t0"] > RETRY_WINDOW_S:
                del self.retries[serial]
                continue
            if now - r["last"] < RETRY_GAP_S:
                continue
            live = {sid for sid in r["failed"] if sid in self.pusher.store.subs and r["tries"].get(sid, 0) < RETRY_MAX_ATTEMPTS}
            if not live:
                del self.retries[serial]
                continue
            r["last"] = now
            res = await self.pusher.broadcast_results(r["kind"], r["payload"], r["critical"], only=live, ttl=r["ttl"])
            for sid in live:
                r["tries"][sid] += 1
            r["failed"] = {sid for sid, x in res.items() if x == "retry" and r["tries"][sid] < RETRY_MAX_ATTEMPTS}
            self.sent += sum(1 for x in res.values() if x == "ok")
            if not r["failed"]:
                del self.retries[serial]

    async def tick(self):
        now = self.clock()
        cur = await self.state_fn(None)
        if self.contract_fn and self.wants("regime") and now - self.last_regime_poll >= 60.0:
            self.last_regime_poll = now
            cid = self.contract_fn()
            if cid:
                try:
                    cur = {**cur, "regime": (await self.state_fn(cid)).get("regime")}
                except Exception:  # noqa: BLE001 - regime is optional; the main poll still stands
                    pass
        extras = self.extras_fn() if self.extras_fn else {}
        events = self.det.step(cur, extras, now)
        seeded = bool(((cur or {}).get("feed") or {}).get("seeded"))
        if self.t0 is None:
            self.t0 = now
        baseline = False
        if seeded and self.armed_at is None:
            self.armed_at = now          # the first seeded poll is the baseline: whatever already exists is not news
            baseline = True
        if now - self.t0 < self.grace_s:
            return []                    # brief grace after boot: a restart must not replay old state as new events
        await self._retry(now)
        sent = []
        for ev in events:
            if ev.kind not in ("feed", "halt") and (not seeded or baseline):
                continue                 # positions and fills are not known until seeded, so nothing is "new" yet
            self.log.append(ev)
            if not self.pusher.store.subs or not self.wants(ev.kind):
                continue
            if not self.limiter.allow(ev.key, now, _contract_of(ev.key), ev.kind):
                continue
            payload = {"title": TITLE, "body": ev.text, "tag": ev.key, "kind": ev.kind}
            ttl = FILL_TTL_S if ev.kind == "fill" and not ev.critical else 600
            res = await self.pusher.broadcast_results(ev.kind, payload, ev.critical, ttl=ttl)
            self.sent += sum(1 for r in res.values() if r == "ok")
            failed = {sid for sid, r in res.items() if r == "retry"}
            if failed and (ev.kind == "fill" or ev.critical):
                self._serial += 1
                self.retries[self._serial] = {"kind": ev.kind, "payload": payload, "critical": ev.critical, "ttl": ttl, "t0": now,
                                              "last": now, "failed": failed, "tries": {sid: 1 for sid in failed}}
            sent.append(ev)
        kind = self.limiter.held_kind
        if self.limiter.held and kind and self.pusher.store.subs and self.wants(kind):
            n = self.limiter.take_held(now)
            if n:
                summary = Event(kind, "summary", f"{n} alerts held back")
                self.log.append(summary)
                res = await self.pusher.broadcast_results(kind, {"title": TITLE, "body": summary.text, "tag": summary.key, "kind": kind}, False)
                self.sent += sum(1 for r in res.values() if r == "ok")
                sent.append(summary)
        return sent


async def notify_loop(notifier, interval=5.0):
    while True:
        try:
            await notifier.tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the notifier must never stop, and never takes the desk with it
            print(f"notify: {type(e).__name__}", flush=True)
        await asyncio.sleep(interval)


def config(environ=None):
    env = os.environ if environ is None else environ
    amounts = (env.get("PX_PUSH_AMOUNTS") or "").strip().lower() in ("1", "true", "yes")
    try:
        grace = max(0.0, min(float(env.get("PX_PUSH_GRACE_S") or 10), 300.0))
    except ValueError:
        grace = 10.0
    try:
        interval = max(2.0, min(float(env.get("PX_PUSH_INTERVAL_S") or 5), 60.0))
    except ValueError:
        interval = 5.0
    return amounts, grace, interval
