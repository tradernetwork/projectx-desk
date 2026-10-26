"""Tomorrow's Map grader: the "LAST CALL" and the running record. Pure, no I/O. Display and logging only.

Ported from the author's TraderMatrix grader (backend/src/services/share/tomorrowMapGrade.ts, itself the mjs `gradeEntry`), pinned to it
by tests/test_tomorrow_grade.py on a grid of 432 map/session pairs whose reference output was produced by running the TypeScript itself.
Do not "improve" a rule here without moving it in the source too, and bump GRADE_VERSION when you do: the record only pools rows graded
under one version.
  - Levels are scored on the CLOSE, not the wick. A wick through that closes back is a hold ("wicked ... closed under").
  - A level price never went near is UNTESTED and stays out of the ratio: a quiet day "holding" a far-away wall is something round numbers
    score just as well.
  - The flip is a ceiling when spot is under it and a floor when over it. A flip sitting on price is not graded (the map made no claim).
  - Scored: flip, BRAKE, CEILING (high side), THIN CUSHION (low side). The PUT WALL is "a crowd, not a floor": reported, never scored.
    ACCELERANT / TRAPDOOR / LAST SHELF are road triggers, not holds.
  - The branch (UP / DOWN / RANGE / WHIPSAW) is read off the map's own first level each way and REPORTED, never scored: the map lays out
    all three roads and does not pick one.
  - RANGE days score the pin: closed within half a typical day of it.
  - Every scored level is paired with a round-number baseline scored by the same rule. A hit rate with nothing to compare it to is not a record.
  - No rate is shown until MIN_GRADED_SESSIONS sessions are graded: the count, never the rate.
A "session bar" is the regular-session {o, h, l, c} of the contract the map was drawn for, in the same (futures) space as the map."""
import math
import re

from projectx_desk.tomorrow import VERSION as MAP_VERSION, _jsround, _px

GRADE_VERSION = 1
MIN_GRADED_SESSIONS = 10

_SUFFIX = (("accel", re.compile(r" ACCELERANT$"), "UP"), ("brake", re.compile(r" BRAKE$"), "UP"), ("gate", re.compile(r" CEILING$"), "UP"),
           ("shelf", re.compile(r" (LAST SHELF|TRAPDOOR)$"), "DOWN"), ("cushion", re.compile(r" THIN CUSHION$"), "DOWN"),
           ("wall", re.compile(r" PUT WALL$"), "DOWN"), ("pin", re.compile(r" PIN$"), "RANGE"))


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _r2(v):
    return _jsround(v * 100) / 100


def extract_levels(m):
    """The named levels a map claims, read back off its own waypoints: the tags ARE the claim, so the grade scores what was drawn."""
    out = {"flip": m["flip"] if m.get("flip") is not None and not m.get("flip_on_price") else None,
           "accel": None, "brake": None, "gate": None, "shelf": None, "cushion": None, "wall": None, "pin": None}
    for key, rx, road_key in _SUFFIX:
        road = next((r for r in m["roads"] if r["key"] == road_key), None)
        wp = next((w for w in (road["waypoints"] if road else []) if rx.search(w["tag"])), None)
        if wp and _fin(wp["price"]):
            out[key] = wp["price"]
    return out


def road_triggers(levels, spot):
    ups = [v for v in (levels["flip"], levels["brake"], levels["gate"]) if v is not None and v > spot]
    dns = [v for v in (levels["shelf"], levels["cushion"], levels["flip"], levels["wall"]) if v is not None and v < spot]
    return {"up": min(ups) if ups else None, "down": max(dns) if dns else None}


def baseline_step(spot):
    """Round-number step for the baseline: a 'nice' 1 / 2 / 2.5 / 5 x 10^k near 0.75% of price (ties to the larger step)."""
    raw = spot * 0.0075
    mag = 10 ** math.floor(math.log10(raw))
    nice = sorted((n * mag for n in (1, 2, 2.5, 5, 10)), key=lambda a: (abs(a - raw), -a))[0]
    return _jsround(nice * 10_000) / 10_000


def grade_map(m, bar):
    """Grade one map against the regular-session bar of the session it was drawn for. `m['spot']` is spot when it was built."""
    spot = m["spot"]
    high, low, close = bar["h"], bar["l"], bar["c"]
    levels = extract_levels(m)
    tol = (m["typical_move"] if m["typical_move"] > 0 else spot * 0.006) * 0.25
    checks = []

    def level(lvl, side, label):
        if lvl is None:
            return
        reached = low if side == "low" else high
        wick_held = low >= lvl if side == "low" else high <= lvl
        held = close >= lvl if side == "low" else close <= lvl
        dist = abs(reached - lvl)
        checks.append({"lvl": lvl, "side": side, "label": label, "dist": _r2(dist), "thru": _r2(lvl - low if side == "low" else high - lvl),
                       "tested": dist <= tol or not wick_held, "ok": held, "wick_held": wick_held})

    if levels["flip"] is not None:
        level(levels["flip"], "high" if spot < levels["flip"] else "low", f"{_px(levels['flip'])} flip")
    level(levels["cushion"], "low", f"{_px(levels['cushion']) if levels['cushion'] is not None else ''} cushion")
    level(levels["brake"], "high", f"{_px(levels['brake']) if levels['brake'] is not None else ''} brake")
    level(levels["gate"], "high", f"{_px(levels['gate']) if levels['gate'] is not None else ''} ceiling")
    wall_tagged = levels["wall"] is not None and low <= levels["wall"] + tol

    trig = road_triggers(levels, spot)
    went_up = trig["up"] is not None and high > trig["up"]
    went_down = trig["down"] is not None and low < trig["down"]
    branch = "WHIPSAW" if went_up and went_down else "UP" if went_up else "DOWN" if went_down else "RANGE"

    R = baseline_step(spot)
    null_hits = []
    n_up = sum(1 for c in checks if c["side"] == "high")
    n_dn = sum(1 for c in checks if c["side"] == "low")
    for i in range(n_up):
        v = math.ceil(spot / R) * R + R * i
        if high >= v - tol:
            null_hits.append(close <= v)
    for i in range(n_dn):
        v = math.floor(spot / R) * R - R * i
        if low <= v + tol:
            null_hits.append(close >= v)

    if branch == "RANGE" and levels["pin"] is not None:
        near = tol * 2
        d = abs(close - levels["pin"])
        checks.append({"lvl": levels["pin"], "side": "pin", "label": f"{_px(levels['pin'])} pin", "dist": _r2(d), "thru": 0, "tested": True,
                       "ok": d <= near, "wick_held": True})
        r = _jsround(spot / R) * R
        null_hits.append(abs(close - r) <= near)

    first_reached = None
    if branch in ("UP", "DOWN"):
        road = next((r for r in m["roads"] if r["key"] == branch), None)
        stops = sorted((w for w in (road["waypoints"] if road else []) if w["tag"] != "EMPTY" and (w["price"] > spot if branch == "UP" else w["price"] < spot)),
                       key=lambda w: abs(w["price"] - spot))
        if stops:
            first_reached = high >= stops[0]["price"] if branch == "UP" else low <= stops[0]["price"]
    elif branch == "RANGE" and levels["pin"] is not None:
        first_reached = low <= levels["pin"] <= high

    band = next((r for r in m["roads"] if r["key"] == "RANGE"), {}).get("band")
    close_vs_band = None if not band else "below" if close < band["lo"] else "above" if close > band["hi"] else "inside"

    tested = [c for c in checks if c["tested"]]
    untested = len(checks) - len(tested)

    def verb(c):
        if c["side"] == "pin":
            return f"settled {c['dist']:.2f} from the {c['label']}" if c["ok"] else f"MISS closed {c['dist']:.2f} from the {c['label']}"
        if c["ok"] and not c["wick_held"]:
            return f"wicked {c['thru']:.2f} through {c['label']}, closed {'under' if c['side'] == 'high' else 'over'}"
        return f"held {c['label']} by {c['dist']:.2f}" if c["ok"] else f"MISS closed through {c['label']}"

    parts = ["whipsawed both roads" if branch == "WHIPSAW" else f"took the {'ELSE' if branch == 'RANGE' else branch} road",
             *[verb(c) for c in tested],
             f"tagged the {_px(levels['wall'])} put wall" if wall_tagged and levels["wall"] is not None else "",
             f"{untested} level{'s' if untested > 1 else ''} untested" if untested else ""]
    return {"branch": branch, "up_trigger": trig["up"], "down_trigger": trig["down"], "first_waypoint_reached": first_reached,
            "close_vs_band": close_vs_band, "hits": sum(1 for c in tested if c["ok"]), "total": len(tested), "untested": untested,
            "null_hits": sum(1 for h in null_hits if h), "null_total": len(null_hits), "wall_tagged": wall_tagged, "checks": checks,
            "summary": " · ".join(p for p in parts if p)}


def record(graded):
    """The running record from graded rows [{session, map_version, grade_version, grade}], oldest first. Pools only the newest map
    version and the current grade version; `publishable` is recomputed from the count, never trusted."""
    rows = [g for g in graded if g.get("grade_version") == GRADE_VERSION and isinstance(g.get("grade"), dict)]
    if not rows:
        return {"last_call": None, "record": {"sessions": 0, "hits": 0, "total": 0, "null_hits": 0, "null_total": 0,
                                              "min_sessions": MIN_GRADED_SESSIONS, "publishable": False, "map_version": MAP_VERSION}}
    ver = max(g.get("map_version") or 0 for g in rows)
    pool = [g for g in rows if (g.get("map_version") or 0) == ver]
    last = max(pool, key=lambda g: g["session"])
    sums = lambda k: sum(int(g["grade"].get(k) or 0) for g in pool)
    return {"last_call": {"session": last["session"], "branch": last["grade"]["branch"], "hits": last["grade"]["hits"], "total": last["grade"]["total"],
                          "untested": last["grade"]["untested"], "summary": str(last["grade"]["summary"])[:200]},
            "record": {"sessions": len(pool), "hits": sums("hits"), "total": sums("total"), "null_hits": sums("null_hits"), "null_total": sums("null_total"),
                       "min_sessions": MIN_GRADED_SESSIONS, "publishable": len(pool) >= MIN_GRADED_SESSIONS, "map_version": ver}}
