"""Thesis card: the trader's stated idea for a trade, held to plain numbers.

The trader taps a type (reversal hold, scalp, fade the range, trend add), a target, an invalidation level and a time limit. Each
card is appended to `data/thesis.jsonl` (`PX_THESIS_FILE`, append-only). Pure checks then report FACTS against it: the invalidation
level has traded through, a scalp has run past its limit, the stop sits wider than the invalidation. Nothing here grades the idea,
advises, or reaches the guard, the client or the hubs; a test pins that. Allowlisted fields only: enums, finite numbers, a contract id.
Files are written with `print(..., file=f)` (the guard pin bans the bare write attribute outside guard.py).
"""
import json
import math
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPES = ("REVERSAL_HOLD", "SCALP", "FADE_RANGE", "TREND_ADD")
SIDES = ("LONG", "SHORT")
CONTRACT = re.compile(r"^CON\.F\.US\.[A-Z0-9]{1,8}\.[A-Z0-9]{1,8}$")
MAX_LIMIT_S = 8 * 3600
PRESETS = {                       # what a tap prefills in the UI; the trader still confirms every number
    "REVERSAL_HOLD": {"limit_s": 3600}, "SCALP": {"limit_s": 300}, "FADE_RANGE": {"limit_s": 1800}, "TREND_ADD": {"limit_s": 3600}}
KEEP = 2000                       # newest rows kept in memory


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def clean(raw, now_ms=None):
    """A request body reduced to a thesis row, or None. Unknown keys are dropped. `side` may be omitted when target and invalidation
    imply it (target above invalidation = LONG); if both are given they must agree with it."""
    if not isinstance(raw, dict):
        return None
    ty, cid = raw.get("type"), raw.get("contract")
    if ty not in TYPES or not isinstance(cid, str) or not CONTRACT.match(cid):
        return None
    tgt, inv, lim = _num(raw.get("target")), _num(raw.get("invalidation")), raw.get("limit_s")
    for given, v in ((raw.get("target"), tgt), (raw.get("invalidation"), inv)):
        if given is not None and (v is None or v <= 0):
            return None
    if lim is not None:
        lim = _num(lim)
        if lim is None or not 1 <= lim <= MAX_LIMIT_S:
            return None
        lim = int(lim)
    side = raw.get("side")
    if side is not None and side not in SIDES:
        return None
    if tgt is not None and inv is not None:
        if tgt == inv:
            return None
        implied = "LONG" if tgt > inv else "SHORT"
        if side is not None and side != implied:
            return None
        side = implied
    return {"ts": int(now_ms if now_ms is not None else time.time() * 1000), "contract": cid, "type": ty, "side": side,
            "target": tgt, "invalidation": inv, "limit_s": lim}


class Store:
    """Append-only thesis ledger. A restart reloads it; a bad line is skipped; the file is never rewritten."""

    def __init__(self, path):
        self.path = Path(path)
        self.rows = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            r = clean(d)
            ts = d.get("ts") if isinstance(d, dict) else None
            if r and isinstance(ts, int) and not isinstance(ts, bool):
                r["ts"] = ts
                self.rows.append(r)
        self.rows = self.rows[-KEEP:]

    def add(self, row):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            print(json.dumps(row, separators=(",", ":")), file=f)
        self.rows.append(row)
        del self.rows[:-KEEP]
        return row

    def latest(self, contract):
        for r in reversed(self.rows):
            if r["contract"] == contract:
                return r
        return None


def store_path(root=None):
    return os.getenv("PX_THESIS_FILE") or Path(root or ROOT) / "data" / "thesis.jsonl"


def check(th, last=None, stop=None, now_ms=None):
    """Facts about one thesis against the market now. `last` is the last price, `stop` the working protective stop price (or None).
    Every flag is a boolean, or None when its input is missing; none of them says the idea was good or bad."""
    side, inv, lim = th.get("side"), th.get("invalidation"), th.get("limit_s")
    last, stop = _num(last), _num(stop)
    broken = None
    if inv is not None and last is not None and side in SIDES:
        broken = last <= inv if side == "LONG" else last >= inv
    elapsed = None if now_ms is None else max(0, int((now_ms - th["ts"]) / 1000))
    overran = None
    if th.get("type") == "SCALP" and lim is not None and elapsed is not None:
        overran = elapsed > lim
    wider = None
    if inv is not None and stop is not None and side in SIDES:
        wider = stop < inv if side == "LONG" else stop > inv
    return {"invalidation_broken": broken, "scalp_overran": overran, "stop_wider_than_invalidation": wider,
            "elapsed_s": elapsed, "limit_s": lim}


def facts(th, chk):
    """The flags that are TRUE as fixed sentences with the numbers beside them. Empty when nothing is true."""
    out = []
    if chk["invalidation_broken"]:
        out.append(f"Price is through your invalidation {th['invalidation']:g}.")
    if chk["scalp_overran"]:
        out.append(f"Scalp is {chk['elapsed_s']}s old, limit was {chk['limit_s']}s.")
    if chk["stop_wider_than_invalidation"]:
        out.append(f"Stop is beyond your invalidation {th['invalidation']:g}.")
    return out


def trip_line(th, trip):
    """Thesis against what happened on one round trip, as one plain line. Prices compared only when the trip carries them."""
    side, tgt, inv, lim = th.get("side") or trip["side"], th.get("target"), th.get("invalidation"), th.get("limit_s")
    parts = [th["type"].replace("_", " ").lower(), f"target {tgt:g}" if tgt is not None else "no target",
             f"invalidation {inv:g}" if inv is not None else "no invalidation", f"limit {lim}s" if lim else "no limit"]
    ex, ent = trip.get("avg_exit"), trip.get("avg_entry")
    out = []
    if trip.get("hold_s") is not None:
        out.append(f"held {trip['hold_s']}s" + (" (over the limit)" if lim and trip["hold_s"] > lim else ""))
    if ex is not None and tgt is not None:
        out.append("exit at or past target" if (ex >= tgt if side == "LONG" else ex <= tgt) else "exit short of target")
    if ex is not None and inv is not None:
        out.append("exit through invalidation" if (ex <= inv if side == "LONG" else ex >= inv) else "exit inside invalidation")
    if ent is not None and inv is not None and (ent <= inv if side == "LONG" else ent >= inv):
        out.append("entry already through invalidation")
    if trip.get("net") is not None:
        out.append(f"net {'-' if trip['net'] < 0 else '+'}${abs(trip['net']):.2f}")
    return "; ".join(parts) + " -> " + (", ".join(out) or "no result yet")


def attach(trips, rows, before_ms=30 * 60 * 1000):
    """Join each trip with the newest thesis on its contract made from `before_ms` before the entry up to the exit (or now, for an
    open trip). A thesis belongs to one trip only (the earliest it fits), so a reused card never decorates the next trade."""
    used = set()
    for t in trips:
        t["thesis"] = None
        end = t["exit_ts"] if t.get("exit_ts") is not None else float("inf")
        best = None
        for r in rows:
            if r["contract"] == t["contract"] and t["entry_ts"] - before_ms <= r["ts"] <= end and r["ts"] not in used \
                    and (best is None or r["ts"] > best["ts"]):
                best = r
        if best:
            used.add(best["ts"])
            t["thesis"] = best
    return trips
