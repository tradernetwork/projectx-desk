#!/usr/bin/env python3
"""Read-only report for the sim probe in docs/SIM_PROBE.md.

Reads the desk's append-only audit (`data/audit.jsonl`, or `--audit FILE`) and, optionally, an order-search reply saved by hand
(`--orders FILE`, the JSON of `POST /api/Order/search`: `{"orders": [...]}` or a bare list), and prints one row per runbook step:
PASS, FAIL or UNKNOWN, with n beside every count.

- UNKNOWN whenever the evidence is absent. A pass is never inferred from silence.
- Prints counts and fixed enums only: never an account id, a balance, an order id, a ticket hash or an order tag.
- Standalone, stdlib only, read-only. Not imported by the desk and wired into no route (pinned by tests/test_probe_report.py).
- Exit 0: the table was printed. Exit 2: bad input (a missing or unreadable file, an orders file that is not order-search JSON,
  a malformed --ticket binding).

Steps that need one specific ticket (S5 inside-the-spread limit, S11 trail entry, S12 one-lot) are bound with
`--ticket S11=<hash8>`; the hash is the 8 characters the desk shows on the ticket. It is matched, never printed.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_AUDIT = ROOT / "data" / "audit.jsonl"
PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
BINDABLE = ("S2L", "S2S", "S5", "S11", "S12")
HASH8 = re.compile(r"^[0-9a-f]{8}$")
OWN_TAG = re.compile(r"^px-[0-9a-f]{8}-\d$")
# Halt codes the guard and the app write (trip_halt, _naked). Anything else prints as "other": the audit may carry any safe string,
# and only a fixed word may leave this script.
HALT_CODES = {
    "unknown_order", "rejected_but_filled", "owned_position_stop_missing", "unprotected_position", "gateway_logout", "stop_failed",
    "stop_unverified_after_close", "stop_orphan_after_close", "late_stop_cancelled", "late_stop_gone", "late_stop_orphan",
    "trail_resize", "stop_resize_failed", "stop_timeout", "naked_position", "flatten_failed", "cancel_unknown", "trail_unknown",
    "trail_unverified", "trail_orphan", "mcp", "manual", "cleared", "position_side_unknown", "stop_lost_after_modify",
}
BENIGN_HALTS = {"manual", "cleared", "gateway_logout"}       # gateway_logout is judged by S1 on its own
CANCEL_CODES = {"user", "limit_ttl", "live_cannot_cancel", "not_entry", "has_fill", "already_cancelled", "protective_stop",
                "not_working", "order_mismatch"}
TRAIL_STATES = ("ARMED", "REJECTED", "NOT_SENT", "UNKNOWN", "UNSEEN", "MISREAD", "CONTEXT_CHANGED", "FLAT_AFTER")


class BadInput(Exception):
    """Fixed message, safe to print."""


# ---- input ------------------------------------------------------------------------------------------------------------------

def read_audit(path):
    """(events, n_lines, n_skipped). A line that is not a JSON object with a string `event` is skipped and counted."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise BadInput("audit file missing or unreadable")
    events, n, skipped = [], 0, 0
    for line in text.splitlines():
        if not line.strip():
            continue
        n += 1
        try:
            rec = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("event"), str):
            skipped += 1
            continue
        events.append(rec)
    return events, n, skipped


def read_orders(path):
    """(rows, n_skipped) from a saved order-search reply. Not JSON, or JSON of the wrong shape, is bad input."""
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise BadInput("orders file missing or unreadable")
    except ValueError:
        raise BadInput("orders file is not JSON")
    rows = raw.get("orders") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise BadInput("orders file is not an order-search reply (no orders list)")
    good = [r for r in rows if isinstance(r, dict)]
    return good, len(rows) - len(good)


def parse_bindings(items):
    out = {}
    for item in items or ():
        step, sep, h = (item or "").partition("=")
        step, h = step.strip().upper(), h.strip().lower()
        if not sep or step not in BINDABLE or not HASH8.match(h):
            raise BadInput("--ticket takes STEP=HASH8, STEP one of " + ", ".join(BINDABLE))
        out[step] = h
    return out


# ---- helpers -----------------------------------------------------------------------------------------------------------------

def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _of(events, event, **match):
    return [e for e in events if e.get("event") == event and all(e.get(k) == v for k, v in match.items())]


def _tickets(events):
    """hash -> list of that ticket's events, in file order."""
    out = {}
    for e in events:
        h = e.get("ticket")
        if isinstance(h, str) and HASH8.match(h):
            out.setdefault(h, []).append(e)
    return out


def _side(evs):
    for e in evs:
        s = e.get("side")
        if s in ("long", "buy"):
            return "long"
        if s in ("short", "sell"):
            return "short"
    return None


def _stop_states(evs):
    return [e.get("state") for e in evs if e.get("event") == "stop"]


def row(step, verdict, what, evidence):
    return (step, verdict, what, evidence)


# ---- steps -------------------------------------------------------------------------------------------------------------------

def s1_logout(events):
    halts = _of(events, "halt")
    idx = [i for i, e in enumerate(halts) if e.get("code") == "gateway_logout"]
    what = "latched gateway_logout halt cleared by a human"
    if not idx:
        return row("S1", UNKNOWN, what, f"no gateway_logout halt in this audit (halt lines n={len(halts)})")
    cleared_after = any(e.get("code") == "cleared" for e in halts[idx[-1] + 1:])
    ev = f"gateway_logout n={len(idx)}, cleared after the last one: {'yes' if cleared_after else 'no'}"
    return row("S1", PASS if cleared_after else FAIL, what, ev)


def s2_entry(events, tix, bind, step, side):
    what = f"market entry ({side}) acked, stop after fill acked"
    if step in bind:
        evs = tix.get(bind[step], [])
        cands = {bind[step]: evs} if evs else {}
    else:
        cands = {h: evs for h, evs in tix.items() if _side(evs) == side and _of(evs, "place")}
    if not cands:
        return row(step, UNKNOWN, what, f"no placed {side} ticket in this audit (bound: {'yes' if step in bind else 'no'})")
    good = bad = 0
    for evs in cands.values():
        st = _stop_states(evs)
        if _of(evs, "unknown") or "FAILED" in st or "TRAIL_MISREAD" in st or _of(evs, "reject", state="REJECTED"):
            bad += 1
        elif _of(evs, "ack") and "ACKED" in st:
            good += 1
    ev = f"tickets n={len(cands)}, acked+stop ACKED n={good}, unknown/failed/rejected n={bad}"
    if bad:
        return row(step, FAIL, what, ev)
    return row(step, PASS if good else UNKNOWN, what, ev)


def s2f_flatten(events):
    fl = _of(events, "flatten")
    flat = sum(1 for e in fl if e.get("state") == "FLAT")
    not_flat = sum(1 for e in fl if e.get("state") == "NOT_FLAT")
    what = "FLATTEN confirms flat"
    ev = f"flatten n={len(fl)}, FLAT n={flat}, NOT_FLAT n={not_flat}"
    if not_flat:
        return row("S2F", FAIL, what, ev)
    return row("S2F", PASS if flat else UNKNOWN, what, ev)


def _cancel_row(step, events, code, what):
    cs = [e for e in _of(events, "cancel") if e.get("code") == code]
    n = {s: sum(1 for e in cs if e.get("state") == s) for s in ("OK", "FAILED", "UNKNOWN", "FILLED")}
    ev = f"cancel code={code} n={len(cs)}: OK n={n['OK']}, FAILED n={n['FAILED']}, UNKNOWN n={n['UNKNOWN']}, FILLED n={n['FILLED']}"
    if n["UNKNOWN"] or (n["FAILED"] and not n["OK"]):
        return row(step, FAIL, what, ev)
    return row(step, PASS if n["OK"] else UNKNOWN, what, ev)


def _desk_refusal(step, events, event, field, value, what):
    n = sum(1 for e in _of(events, event) if e.get(field) == value)
    return row(step, PASS if n else UNKNOWN, what, f"{event} {field}={value} n={n}")


def s5_inside_spread(tix, bind):
    what = "limit inside a wide spread: server accepts or refuses"
    if "S5" not in bind:
        return row("S5", UNKNOWN, what, "not bound: pass --ticket S5=<hash8>")
    evs = tix.get(bind["S5"], [])
    if _of(evs, "unknown"):
        return row("S5", FAIL, what, "the write went UNKNOWN")
    rej = _of(evs, "reject", state="REJECTED")
    if _of(evs, "ack"):
        return row("S5", PASS, what, "settled: the server ACCEPTED it (ack n=1)")
    if rej:
        code = rej[-1].get("code")
        code = code if isinstance(code, str) and re.match(r"^-?\d{1,4}$", code) else "?"
        return row("S5", PASS, what, f"settled: the server REFUSED it (errorCode {code})")
    return row("S5", UNKNOWN, what, f"bound ticket has no ack or broker reject (events n={len(evs)})")


def _by_hand(step, what, why):
    return row(step, UNKNOWN, what, "by hand, not in these files: " + why)


def s8_fields(orders):
    out = []
    for step, field, what in (("S8a", "fillVolume", "order rows carry fillVolume"), ("S8b", "customTag", "order rows carry customTag")):
        if orders is None:
            out.append(row(step, UNKNOWN, what, "no --orders file"))
            continue
        n = len(orders)
        k = sum(1 for o in orders if o.get(field) is not None)
        ev = f"rows n={n}, with {field} n={k}"
        if field == "customTag":
            own = sum(1 for o in orders if isinstance(o.get(field), str) and OWN_TAG.match(o[field]))
            ev += f", desk-shaped n={own}"
        if not n:
            out.append(row(step, UNKNOWN, what, ev))
        else:
            out.append(row(step, PASS if k else FAIL, what, ev))
    return out


def s9_arm_trail(events):
    tr = _of(events, "trail")
    n = {s: sum(1 for e in tr if e.get("state") == s) for s in TRAIL_STATES}
    what = "PX_TRAIL_STOP arm_trail: placed, read back, old stop dropped"
    ev = ", ".join(f"{s} n={n[s]}" for s in TRAIL_STATES if n[s]) or "no trail events"
    ev = f"trail lines n={len(tr)}: {ev}"
    if n["UNSEEN"] or n["UNKNOWN"] or n["MISREAD"]:
        return row("S9", FAIL, what, ev)
    if n["ARMED"]:
        return row("S9", PASS, what, ev)
    if n["REJECTED"]:
        return row("S9", FAIL, what, ev + " (the gateway rejected the type-5 place; old stop kept)")
    return row("S9", UNKNOWN, what, ev)


def s10_trail_fields(orders, tick):
    what = "type-5 rows: trailPrice is the distance, stopPrice finite"
    if orders is None:
        return row("S10", UNKNOWN, what, "no --orders file")
    t5 = [o for o in orders if o.get("type") == 5]
    both = [o for o in t5 if _fin(o.get("trailPrice")) and _fin(o.get("trailDistance"))]
    cons = [o for o in both if abs(o["trailPrice"] - o["trailDistance"] * tick) <= 2 * tick]
    sp = sum(1 for o in t5 if _fin(o.get("stopPrice")) and o["stopPrice"] > 0)
    ev = f"type-5 rows n={len(t5)}, with both fields n={len(both)}, consistent n={len(cons)}, finite stopPrice n={sp}"
    if not t5:
        return row("S10", UNKNOWN, what, ev)
    ok = len(cons) == len(t5) and sp == len(t5)
    return row("S10", PASS if ok else FAIL, what, ev)


def _trail_stop_row(step, evs, what, extra=""):
    st = _stop_states(evs)
    ev = (extra + ", " if extra else "") + f"stop lines n={len(st)}"
    if "TRAIL_FALLBACK_FIXED" in st:
        return row(step, FAIL, what, ev + ": fell back to a FIXED stop (TRAIL_FALLBACK_FIXED)")
    if "TRAIL_MISREAD" in st or "FAILED" in st:
        return row(step, FAIL, what, ev + ": the stop did not read back (TRAIL_MISREAD or FAILED)")
    if "ACKED" in st:
        return row(step, PASS, what, ev + ": acked after the trail read-back")
    return row(step, UNKNOWN, what, ev + ": no acked stop")


def s11_trail_entry(tix, bind):
    what = "PX_TRAIL_ENTRY: the post-fill stop goes in as a TRAIL"
    if "S11" not in bind:
        return row("S11", UNKNOWN, what, "not bound: pass --ticket S11=<hash8> (a ticket placed with PX_TRAIL_ENTRY=1)")
    return _trail_stop_row("S11", tix.get(bind["S11"], []), what)


def s12_one_lot(tix, bind):
    what = "PX_ONE_LOT: 1 MES staged in the 1-lot branch, trail stop after fill"
    if "S12" not in bind:
        return row("S12", UNKNOWN, what, "not bound: pass --ticket S12=<hash8>")
    evs = tix.get(bind["S12"], [])
    if not evs:
        return row("S12", UNKNOWN, what, "bound ticket not in this audit")
    if not any(e.get("event") == "preview" and e.get("one_lot") == 1 for e in evs):
        return row("S12", FAIL, what, "the ticket did not take the 1-lot branch (no preview one_lot=1)")
    return _trail_stop_row("S12", evs, what, "one_lot=1")


def x_halts(events):
    halts = _of(events, "halt")
    counts = {}
    for e in halts:
        c = e.get("code")
        c = c if c in HALT_CODES else "other"
        counts[c] = counts.get(c, 0) + 1
    what = "no halt but manual, cleared or gateway_logout"
    ev = ", ".join(f"{c} n={counts[c]}" for c in sorted(counts)) or "none"
    ev = f"halt lines n={len(halts)}: {ev}"
    if not events:
        return row("X0", UNKNOWN, what, "empty audit")
    bad = [c for c in counts if c not in BENIGN_HALTS]
    return row("X0", FAIL if bad else PASS, what, ev)


def x_unknown(events):
    writes = [e for e in events if e.get("event") in ("place", "stop", "cancel", "trail", "flatten")]
    unk = len(_of(events, "unknown")) + len(_of(events, "cancel", state="UNKNOWN")) + len(_of(events, "trail", state="UNKNOWN"))
    what = "no write went UNKNOWN"
    ev = f"write lines n={len(writes)}, UNKNOWN n={unk}"
    if unk:
        return row("X1", FAIL, what, ev)
    return row("X1", PASS if writes else UNKNOWN, what, ev)


def build(events, orders, tick, bind):
    tix = _tickets(events)
    return [
        s1_logout(events),
        s2_entry(events, tix, bind, "S2L", "long"),
        s2_entry(events, tix, bind, "S2S", "short"),
        s2f_flatten(events),
        _cancel_row("S3", events, "user", "CANCEL button pulls a resting limit"),
        _cancel_row("S4", events, "limit_ttl", "after a reload: no button, the limit TTL pulls it"),
        _desk_refusal("S5a", events, "reject", "check", "limit_rests", "desk refuses a limit at or through the far side"),
        s5_inside_spread(tix, bind),
        _desk_refusal("S6a", events, "reject", "check", "stop_clear_of_market", "desk refuses a stop at or through the book"),
        _by_hand("S6", "gateway reply to a crossed stop", "write down the TopstepX reply"),
        _desk_refusal("S7a", events, "cancel", "code", "has_fill", "desk refuses CANCEL once the entry has a fill"),
        _by_hand("S7", "broker cancel of a partially filled limit", "not reachable at qty 1"),
        *s8_fields(orders),
        s9_arm_trail(events),
        s10_trail_fields(orders, tick),
        s11_trail_entry(tix, bind),
        s12_one_lot(tix, bind),
        _by_hand("S13", "broker modify of a live trail, a trail on a gap", "TopstepX UI only; the desk never modifies a trail"),
        _by_hand("S14", "tick throughput at the Sunday open", "prints per minute, lag, CPU on the sheet"),
        x_halts(events),
        x_unknown(events),
    ]


def render(rows, header):
    for h in header:
        print(h)
    print()
    w = max(len(r[2]) for r in rows)
    print(f"{'STEP':<5} {'VERDICT':<8} {'CHECK':<{w}}  EVIDENCE")
    for step, verdict, what, ev in rows:
        print(f"{step:<5} {verdict:<8} {what:<{w}}  {ev}")
    tally = {v: sum(1 for r in rows if r[1] == v) for v in (PASS, FAIL, UNKNOWN)}
    print()
    print(f"PASS n={tally[PASS]}, FAIL n={tally[FAIL]}, UNKNOWN n={tally[UNKNOWN]} (rows n={len(rows)})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only pass / fail / UNKNOWN table for docs/SIM_PROBE.md.")
    ap.add_argument("--audit", default=str(DEFAULT_AUDIT), help="the desk's audit JSONL (default data/audit.jsonl)")
    ap.add_argument("--orders", help="a saved POST /api/Order/search reply (JSON)")
    ap.add_argument("--tick", type=float, default=0.25, help="tick size of the probed contract (MES 0.25)")
    ap.add_argument("--ticket", action="append", metavar="STEP=HASH8", help="bind a step to one ticket: " + ", ".join(BINDABLE))
    a = ap.parse_args(argv)
    try:
        if not (_fin(a.tick) and a.tick > 0):
            raise BadInput("--tick must be a positive number")
        bind = parse_bindings(a.ticket)
        events, n, skipped = read_audit(a.audit)
        orders, oskip = read_orders(a.orders) if a.orders else (None, 0)
    except BadInput as e:
        print(f"bad input: {e}", file=sys.stderr)
        return 2
    header = [f"audit: lines n={n}, parsed n={len(events)}, skipped (garbled) n={skipped}",
              (f"orders: rows n={len(orders)}, skipped (not an object) n={oskip}" if orders is not None else "orders: none given"),
              f"bound steps: {', '.join(sorted(bind)) or 'none'}"]
    render(build(events, orders, a.tick, bind), header)
    return 0


if __name__ == "__main__":
    sys.exit(main())
