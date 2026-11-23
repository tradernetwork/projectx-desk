"""Trade-event detector: notices five habits the moment they happen and says what happened. READ-ONLY and display only.

It is given the desk's own fills, positions and working orders (injected snapshots, same as notify.py) and a function that returns
the BIAS log. It imports nothing from the guard, the client, the hub, the MCP tools or the app, and has no way to place, cancel,
flatten, halt or clear anything. Output is a short list of events built from fixed templates and validated enums: a contract ROOT
that passes a strict pattern, LONG/SHORT, a grade letter, a clock time and whole seconds or minutes. Never an account id, balance,
dollar amount, order tag or broker free text. An event is a fact ("MES entry 3m 00s after a losing exit"), never advice and never a
signal to act.

The habit definitions are journal.py's (REVENGE_S, LATE_HM, `is_revenge`, `is_late`, `attach_context`, `round_trips`), so the live
alert and the trade memory can never disagree about what counts. The one habit journal.py has no word for is the missing stop, which
uses state.is_protective_stop, the one helper the whole desk uses to decide what counts as protection.

Kinds: NO_STOP, REENTRY, SCALE_IN, LATE_ENTRY, COUNTER_BIAS, STOP_WIDENED, CORRELATED (Round 39).
STOP_WIDENED: a working fixed stop (type 4) on an open position whose trigger moved AWAY from price since the detector last saw that same
order id (long: lower, short: higher). The desk itself only ever tightens, so this is the trader's change in the broker UI. A TRAIL's trigger
moves by design and is never compared; a replaced order (new id) is a new baseline, not a widening. Text carries the points moved, no dollars.
CORRELATED: positions open on two or more roots of one group in CORR_GROUPS at once (the one table). Said once per set of roots.
Config (env): PX_TRADEWATCH (default OFF, `1` turns it on), PX_TRADEWATCH_NOSTOP_S (default 15, clamped 3 to 300, garbled = 15).
The first seeded look is a silent baseline: whatever already exists is not news, and a fill older than FRESH_S is never announced.
"""
import os
import re
import time
from datetime import datetime

from . import journal
from .notify import root_of
from .state import is_protective_stop

KINDS = ("NO_STOP", "REENTRY", "SCALE_IN", "LATE_ENTRY", "COUNTER_BIAS", "STOP_WIDENED", "CORRELATED")
# Roots that move together, so open positions on two of them are one exposure counted twice against a shared loss room. The ONE table.
CORR_GROUPS = {"EQUITY_INDEX": ("MES", "MNQ", "M2K", "MYM", "ES", "NQ", "RTY", "YM")}
FIXED_STOP_TYPE = 4
DEFAULT_NOSTOP_S = 15
FRESH_S = 120            # an entry older than this when first seen (a reseed bringing in history) is not announced
KEEP = 20                # events kept for the page and the digest
SHOW_S = 600             # an event stays in the snapshot this long; the page also dedupes by id
_GRADE = re.compile(r"^[A-F][+-]?$")
_DIRS = ("LONG", "SHORT")


def _word(cid):
    return root_of(cid) or "contract"


def _clock(ms):
    return datetime.fromtimestamp(ms / 1000, journal.CT).strftime("%H:%M")


def _span(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def config(environ=None):
    """(enabled, nostop_s). Off unless PX_TRADEWATCH is exactly a yes-word; a garbled delay falls back to the default."""
    env = os.environ if environ is None else environ
    on = (env.get("PX_TRADEWATCH") or "").strip().lower() in ("1", "true", "yes")
    try:
        n = float(env.get("PX_TRADEWATCH_NOSTOP_S") or DEFAULT_NOSTOP_S)
        n = n if n == n else DEFAULT_NOSTOP_S
    except ValueError:
        n = DEFAULT_NOSTOP_S
    return on, max(3.0, min(n, 300.0))


class TradeWatch:
    def __init__(self, nostop_s=DEFAULT_NOSTOP_S, bias_fn=None, fresh_s=FRESH_S):
        self.nostop_s, self.bias_fn, self.fresh_ms = nostop_s, bias_fn, fresh_s * 1000
        self.primed = False
        self.seen_ids, self.known = set(), {}
        self.uncovered = {}                  # contract -> (first seen uncovered, told)
        self.stops = {}                      # (contract, order id) -> last seen trigger of a working fixed stop
        self.corr_told = set()               # (group, roots) already announced
        self.corr_now = []                   # current groups, for the snapshot
        self.events = []

    # -- output --------------------------------------------------------------------------------------------------------
    def snapshot(self, now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        ev = [e for e in self.events if now_ms - e["ts"] <= SHOW_S * 1000]
        return {"status": "OK" if self.primed else "WAIT", "nostop_s": self.nostop_s, "correlated": [dict(c) for c in self.corr_now],
                "events": [dict(e) for e in ev]}

    def _emit(self, out, kind, cid, ts, text):
        key = f"{kind}:{cid}:{ts}"
        if any(e["id"] == key for e in self.events):
            return
        e = {"id": key, "kind": kind, "ts": ts, "text": text}
        self.events.append(e)
        del self.events[:-KEEP]
        out.append(e)

    # -- the step ------------------------------------------------------------------------------------------------------
    def step(self, fills, positions, orders, seeded, now_s, now_ms):
        """fills: raw GatewayUserTrade dicts; positions and orders: the live raw dicts. seeded: positions, orders and fills all read.
        now_s is a monotonic clock for the stop timer, now_ms wall time for fill ages. Returns the NEW events."""
        out = []
        if not seeded:
            self.uncovered.clear()           # an unreadable book proves nothing about stops
            self.stops.clear()               # nor about a stop's last trigger: the next readable look is a new baseline
            self.corr_now = []
            return out
        rows = journal.fill_rows(fills)
        trips = journal.round_trips(rows)
        known = {(t["contract"], t["entry_ts"]): t["n_entries"] for t in trips}
        new_rows = [r for r in rows if r["id"] not in self.seen_ids]
        self.seen_ids = {r["id"] for r in rows}
        if not self.primed:                  # first seeded look: baseline only
            self.primed, self.known = True, known
            self._stops(positions, orders, now_s, now_ms, out, baseline=True)
            self._widened(positions, orders, now_ms, out, baseline=True)
            self._correlated(positions, now_ms, out, baseline=True)
            return out
        fresh = {r["contract"] for r in new_rows if now_ms - r["ts"] <= self.fresh_ms}
        if fresh:
            self._entries(trips, fresh, now_ms, out)
        self.known = known
        self._stops(positions, orders, now_s, now_ms, out)
        self._widened(positions, orders, now_ms, out)
        self._correlated(positions, now_ms, out)
        return out

    def _entries(self, trips, fresh, now_ms, out):
        bias, last_loss, opened = None, {}, []
        for t in trips:                      # oldest first, like journal.summarize
            c, key = t["contract"], (t["contract"], t["entry_ts"])
            if key not in self.known and c in fresh and now_ms - t["entry_ts"] <= self.fresh_ms:
                opened.append((t, last_loss.get(c)))
            elif key in self.known and t["n_entries"] > self.known[key] and c in fresh:
                self._emit(out, "SCALE_IN", c, now_ms, f"{_word(c)} {t['side']} added to: entry {t['n_entries']} in the same position")
            if t["net"] is not None and t["net"] < 0:
                last_loss[c] = t["exit_ts"]
        for t, ll in opened:
            c, ts, w = t["contract"], t["entry_ts"], _word(t["contract"])
            if journal.is_revenge(ts, ll):
                self._emit(out, "REENTRY", c, ts, f"{w} {t['side']} entry {_span((ts - ll) / 1000)} after a losing exit on {w}")
            if journal.is_late(ts):
                self._emit(out, "LATE_ENTRY", c, ts, f"{w} {t['side']} entry at {_clock(ts)} CT, after {journal.LATE_HM[0]}:{journal.LATE_HM[1]:02d}")
            if bias is None and self.bias_fn:
                try:
                    bias = self.bias_fn() or {}
                except Exception:  # noqa: BLE001 - the BIAS log is optional context
                    bias = {}
            ctx = journal.attach_context([dict(t)], bias)[0].get("ctx") if bias else None
            if ctx and ctx.get("aligned") is False and ctx.get("direction") in _DIRS:
                g = ctx.get("grade") if isinstance(ctx.get("grade"), str) and _GRADE.match(ctx["grade"]) else None
                self._emit(out, "COUNTER_BIAS", c, ts, f"{w} {t['side']} entry against the BIAS read ({ctx['direction']}" + (f", grade {g})" if g else ")"))

    def _stops(self, positions, orders, now_s, now_ms, out, baseline=False):
        ords = [o for o in (orders or []) if isinstance(o, dict)]
        seen = set()
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            cid, size, side = p.get("contractId"), p.get("size"), {1: "long", 2: "short"}.get(p.get("type"))
            if not isinstance(cid, str) or side is None or not isinstance(size, (int, float)) or isinstance(size, bool) or not size > 0:
                continue
            seen.add(cid)
            if any(is_protective_stop(o, side, size, cid) for o in ords):
                self.uncovered.pop(cid, None)
                continue
            since, told = self.uncovered.get(cid, (now_s, False))
            if not told and not baseline and now_s - since >= self.nostop_s:
                told = True
                self._emit(out, "NO_STOP", cid, now_ms, f"{_word(cid)} {side.upper()} has no working stop order {_span(now_s - since)} after it was seen open")
            self.uncovered[cid] = (since, told)
        for cid in list(self.uncovered):
            if cid not in seen:
                del self.uncovered[cid]

    def _widened(self, positions, orders, now_ms, out, baseline=False):
        """A working fixed stop whose trigger moved away from price since the last look. Unreadable anything: no comparison, no event."""
        ords = [o for o in (orders or []) if isinstance(o, dict)]
        cur = {}
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            cid, size, side = p.get("contractId"), p.get("size"), {1: "long", 2: "short"}.get(p.get("type"))
            if not isinstance(cid, str) or side is None or not _pos(size):
                continue
            for o in ords:
                oid, trig = o.get("id"), o.get("stopPrice")
                if o.get("type") != FIXED_STOP_TYPE or not isinstance(oid, int) or isinstance(oid, bool) or not _pos(trig):
                    continue
                if not is_protective_stop(o, side, size, cid):
                    continue
                key = (cid, oid)
                cur[key] = trig
                was = self.stops.get(key)
                if baseline or was is None or not _pos(was):
                    continue
                away = (was - trig) if side == "long" else (trig - was)
                if away > 1e-9:
                    self._emit(out, "STOP_WIDENED", cid, now_ms,
                               f"{_word(cid)} {side.upper()} stop moved away from price by {_pts(away)} points after it was set")
        self.stops = cur

    def _correlated(self, positions, now_ms, out, baseline=False):
        sides = {}
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            side, size, r = {1: "LONG", 2: "SHORT"}.get(p.get("type")), p.get("size"), root_of(p.get("contractId"))
            if side and r and _pos(size):
                sides.setdefault(r, set()).add(side)
        now = []
        for grp, members in CORR_GROUPS.items():
            roots = tuple(sorted(r for r in sides if r in members))
            if len(roots) >= 2:
                now.append({"group": grp, "roots": list(roots)})
                if (grp, roots) not in self.corr_told:
                    self.corr_told.add((grp, roots))
                    if not baseline:
                        txt = " and ".join(f"{r} {'/'.join(sorted(sides[r]))}" for r in roots)
                        self._emit(out, "CORRELATED", grp, now_ms, f"{txt} open at once: both are {grp.replace('_', ' ').lower()} futures")
        self.corr_told = {k for k in self.corr_told if any(c["group"] == k[0] and tuple(c["roots"]) == k[1] for c in now)}
        self.corr_now = now


def _pos(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf")) and v > 0


def _pts(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def digest_lines(tw):
    """Fixed-format lines for /ai/digest. `tw` is a snapshot dict or None. Absent is OFF, never blank."""
    if not isinstance(tw, dict) or tw.get("status") not in ("OK", "WAIT"):
        return ["TRADEWATCH: OFF"]
    ev = tw.get("events") or []
    out = [f"TRADEWATCH: {tw['status']} nostop_s={tw.get('nostop_s')} events={len(ev)}"]
    out += [f"TRADEWATCH_CORR: {c['group']} roots={','.join(c['roots'])} open at once" for c in (tw.get("correlated") or [])
            if isinstance(c, dict) and isinstance(c.get("roots"), list)]
    out += [f"TRADEWATCH_EVENT: {e['kind']} at={_clock(e['ts'])}CT {e['text']}" for e in ev[-5:]]
    if ev:
        out.append("TRADEWATCH_NOTE: facts about how the trade was taken, an alert not a signal; never gates or sizes an order")
    return out
