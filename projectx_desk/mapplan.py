"""Dealer-map event proposals, SHADOW MODE: what plan the desk WOULD have made when price reacted to a map level, graded against what price did.

Pure, no I/O, and no import of the guard, the client or the hubs: nothing here can reach the order path. Never read by the guard, sizing or the
AI surface; nothing is shown to the trader and nothing can be staged from it. Stage 1 of a three-stage process (docs/DECISIONS.md, Round 21):
  1. shadow   log every proposal and grade it, silently          <- this module
  2. display  show it on the chart, labelled not-a-signal         (only after the record beats its baseline)
  3. stage    a button that stages it through the normal guard   (only after that, behind a default-OFF switch, human hold unchanged)

Rules this module keeps (they are the ones the trader set, not defaults to relax):
  - The STOP is the ATR stop (1.5 x ATR, the desk's default chip), never a map or GEX level (Round 6: GEX never anchors a stop or a target).
    The map supplies only the TRIGGER and an ADVISORY target line, never sent anywhere (the guard has no take-profit order).
  - Triggers are read on CLOSED 5m bars only, never a tick, with the closed bar as the dedupe key. Nothing is inferred from the forming bar.
  - A map is used only on the day it was made (the caller checks the snapshot date); a wrong-contract or missing map yields nothing.
  - Entry is the trigger bar's close (the nearest honest stand-in for the next fill); the stop distance is bounded to the guard's 4 to 200 ticks, and
    a plan that needs more is not proposed rather than clamped.
  - Grading is conservative: a bar that touches both stop and target counts as the stop. Every proposal is also scored on the OPPOSITE side with the
    same stop distance, so any record has a baseline to beat. No rate is meaningful under MIN_RESOLVED resolved proposals per kind.
Process, not prediction: this exists so the idea earns its place with data instead of an anecdote (see the BIAS grade, Round 20)."""
import math
import re

STOP_ATR = 1.5                 # the desk's default stop chip
MIN_STOP_TICKS, MAX_STOP_TICKS = 4, 200
HORIZON_BARS = 24              # two hours of 5m bars
FLAT_BY = (15, 8)              # CT: the desk flattens here, so nothing is graded past it
COOLDOWN_S = 30 * 60           # the same (kind, level) is not proposed again inside this
DAILY_CAP = 8                  # proposals per contract per CT day, so chop cannot become a tilt machine
LOOKBACK_RECLAIM = 12          # a reclaim needs a close on the wrong side of the level within this many bars
BAR_MS = 300_000
MIN_RESOLVED = 30

_ROLE = re.compile(r"(?:^|\s)(ACCELERANT|BRAKE|CEILING|TRAPDOOR|LAST SHELF|THIN CUSHION|PUT WALL|PIN|FLIP)$")

# (event, role) -> (kind, side). cross_dn/cross_up are close-through, reject is a wick through that closes back, reclaim is a failed breakdown.
TRIGGERS = {
    ("cross_dn", "TRAPDOOR"): ("TRAPDOOR_LOST", "SHORT"), ("cross_dn", "LAST SHELF"): ("SHELF_LOST", "SHORT"), ("cross_dn", "THIN CUSHION"): ("CUSHION_LOST", "SHORT"),
    ("cross_up", "ACCELERANT"): ("ACCEL_THROUGH", "LONG"), ("cross_up", "BRAKE"): ("BRAKE_CLEARED", "LONG"), ("cross_up", "CEILING"): ("CEILING_CLEARED", "LONG"),
    ("reject", "FLIP"): ("FLIP_REJECT", "SHORT"), ("reject", "BRAKE"): ("BRAKE_REJECT", "SHORT"), ("reject", "CEILING"): ("CEILING_REJECT", "SHORT"),
    ("reclaim", "TRAPDOOR"): ("TRAPDOOR_RECLAIM", "LONG"), ("reclaim", "LAST SHELF"): ("SHELF_RECLAIM", "LONG"),
}
KINDS = sorted({k for k, _ in TRIGGERS.values()})


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _tick_round(x, tick, up=False):
    n = x / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 8)


def levels(m):
    """[{price, role, tail}] read from the map's waypoints (plus the flip when it is below price and so not a waypoint)."""
    out, seen = [], set()
    for r in m.get("roads") or []:
        for w in r.get("waypoints") or []:
            mt = _ROLE.search(str(w.get("tag") or ""))
            if mt and _fin(w.get("price")):
                out.append({"price": float(w["price"]), "role": mt.group(1), "tail": bool(w.get("tail"))})
                seen.add((float(w["price"]), mt.group(1)))
    f = m.get("flip")
    if _fin(f) and not m.get("flip_on_price") and (float(f), "FLIP") not in seen:
        out.append({"price": float(f), "role": "FLIP", "tail": abs(f - (m.get("spot") or f)) > (m.get("typical_move") or 0)})
    return out


def _valid_bars(bars):
    return (isinstance(bars, list) and len(bars) >= 3
            and all(isinstance(b, dict) and all(_fin(b.get(k)) for k in ("timestamp", "high", "low", "close")) for b in bars[-(LOOKBACK_RECLAIM + 2):]))


def detect(m, bars, atr, tick, root=None, contract=None, snapshot_epoch=None):
    """Proposals for the LAST CLOSED bar of `bars` (oldest first, closed bars only). Empty list when nothing fires or anything is unusable."""
    if not isinstance(m, dict) or not _valid_bars(bars) or not _fin(atr) or atr <= 0 or not _fin(tick) or tick <= 0:
        return []
    bar, prev = bars[-1], bars[-2]
    stop_dist = _tick_round(STOP_ATR * atr, tick, up=True)
    ticks = stop_dist / tick
    if ticks < MIN_STOP_TICKS or ticks > MAX_STOP_TICKS:
        return []                                                         # the guard would refuse it: do not propose, do not clamp
    lv = levels(m)
    out = []
    for L in lv:
        p, role = L["price"], L["role"]
        events = []
        if prev["close"] >= p and bar["close"] < p:
            events.append("cross_dn")
        if prev["close"] <= p and bar["close"] > p:
            events.append("cross_up")
            if any(b["close"] < p for b in bars[-(LOOKBACK_RECLAIM + 1):-1]):
                events.append("reclaim")
        if bar["high"] >= p and bar["close"] < p and prev["close"] < p:
            events.append("reject")
        for ev in events:
            hit = TRIGGERS.get((ev, role))
            if not hit:
                continue
            kind, side = hit
            entry = float(bar["close"])
            stop = round(entry + stop_dist if side == "SHORT" else entry - stop_dist, 8)
            beyond = [x for x in lv if x["role"] != "PIN" and ((x["price"] <= entry - stop_dist) if side == "SHORT" else (x["price"] >= entry + stop_dist))]
            tgt = (max(beyond, key=lambda x: x["price"]) if side == "SHORT" else min(beyond, key=lambda x: x["price"])) if beyond else None
            out.append({"id": f"{root}:{int(bar['timestamp'])}:{kind}:{p:g}", "root": root, "contract": contract, "bar_ts": int(bar["timestamp"]), "kind": kind, "side": side,
                        "level": p, "role": role, "entry": entry, "stop": stop, "stop_ticks": int(round(ticks)), "stop_dist": stop_dist,
                        "target": None if tgt is None else tgt["price"], "target_role": None if tgt is None else tgt["role"],
                        "r_target": None if tgt is None else round(abs(entry - tgt["price"]) / stop_dist, 2), "atr": round(atr, 6),
                        "regime": m.get("regime"), "snapshot_epoch": snapshot_epoch})
    return out


def allowed(p, rows, now_ts, ct_date):
    """Cooldown per (kind, level) and a daily cap per contract, against the proposals already logged. `ct_date` is a function ms -> CT date string."""
    same_day = [r for r in rows if r.get("kind_row") == "proposal" and r.get("root") == p["root"] and ct_date(r["bar_ts"]) == ct_date(p["bar_ts"])]
    if len(same_day) >= DAILY_CAP:
        return False
    for r in same_day:
        if r["kind"] == p["kind"] and r["level"] == p["level"] and (p["bar_ts"] - r["bar_ts"]) / 1000 < COOLDOWN_S:
            return False
    return not any(r.get("id") == p["id"] for r in same_day)


def resolve(p, after, flat_by_ms=None, horizon=HORIZON_BARS):
    """Grade a proposal against the closed bars AFTER its trigger bar (oldest first). None until it hits a level or the horizon is complete.
    A bar touching both stop and target counts as the stop. R is in multiples of the stop distance; r_opp is the opposite side, same stop distance,
    no target (the baseline). mfe/mae are the best and worst excursions seen before the exit, in R."""
    if not after:
        return None
    side, entry, stop, tgt, d = p["side"], p["entry"], p["stop"], p.get("target"), p["stop_dist"]
    sgn = -1 if side == "SHORT" else 1
    mfe = mae = 0.0
    opp_stop = entry + sgn * d                      # the opposite side's stop sits on the other side of entry (below it for a short plan)
    opp_r, opp_done = None, False
    for i, b in enumerate(after[:horizon], 1):
        if flat_by_ms is not None and b["timestamp"] >= flat_by_ms(b["timestamp"]):
            return _out(p, "FLATBY", sgn * (after[i - 2]["close"] - entry) / d if i > 1 else 0.0, opp_r if opp_done else _opp(sgn, entry, after[i - 2]["close"] if i > 1 else entry, d), i - 1, mfe, mae)
        fav = sgn * ((b["high"] if sgn > 0 else b["low"]) - entry) / d
        adv = -sgn * ((b["low"] if sgn > 0 else b["high"]) - entry) / d
        mfe, mae = max(mfe, fav), max(mae, adv)
        stop_hit = b["high"] >= stop if sgn < 0 else b["low"] <= stop
        tgt_hit = tgt is not None and (b["low"] <= tgt if sgn < 0 else b["high"] >= tgt)
        if not opp_done:
            if (b["low"] <= opp_stop if sgn < 0 else b["high"] >= opp_stop):
                opp_r, opp_done = -1.0, True
        if stop_hit:
            return _out(p, "STOP", -1.0, opp_r if opp_done else _opp(sgn, entry, b["close"], d), i, mfe, mae, both=tgt_hit)
        if tgt_hit:
            return _out(p, "TARGET", abs(entry - tgt) / d, opp_r if opp_done else _opp(sgn, entry, b["close"], d), i, mfe, mae)
    if len(after) < horizon:
        return None
    last = after[horizon - 1]["close"]
    return _out(p, "TIMEOUT", sgn * (last - entry) / d, opp_r if opp_done else _opp(sgn, entry, last, d), horizon, mfe, mae)


def _opp(sgn, entry, close, d):
    return max(-1.0, -sgn * (close - entry) / d)


def _out(p, outcome, r, r_opp, bars, mfe, mae, both=False):
    return {"id": p["id"], "root": p["root"], "kind": p["kind"], "side": p["side"], "outcome": outcome, "r": round(r, 3), "r_opp": round(r_opp, 3),
            "bars": bars, "mfe_r": round(mfe, 3), "mae_r": round(mae, 3), "both_in_bar": bool(both)}


def stats(props, outs):
    """Per trigger kind: counts, outcome shares, mean R of the plan and of the opposite side. A rate is withheld under MIN_RESOLVED resolved proposals."""
    by_id = {o["id"]: o for o in outs}
    table = {}
    for p in props:
        row = table.setdefault(p["kind"], {"kind": p["kind"], "proposed": 0, "resolved": 0, "stop": 0, "target": 0, "timeout": 0, "r": [], "r_opp": []})
        row["proposed"] += 1
        o = by_id.get(p["id"])
        if not o:
            continue
        row["resolved"] += 1
        row[{"STOP": "stop", "TARGET": "target"}.get(o["outcome"], "timeout")] += 1
        row["r"].append(o["r"])
        row["r_opp"].append(o["r_opp"])
    res = []
    for k in sorted(table):
        t = table[k]
        n = t["resolved"]
        enough = n >= MIN_RESOLVED
        res.append({"kind": k, "proposed": t["proposed"], "resolved": n, "enough": enough,
                    "mean_r": round(sum(t["r"]) / n, 3) if n and enough else None, "mean_r_opp": round(sum(t["r_opp"]) / n, 3) if n and enough else None,
                    "stop_share": round(t["stop"] / n, 3) if n and enough else None, "target_share": round(t["target"] / n, 3) if n and enough else None})
    return res
