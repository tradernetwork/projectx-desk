"""Tomorrow's Map: three roads out of spot (up, down, range) from the dealer-gamma ladder, each waypoint labelled with what dealer
gamma does to price THERE, plus an IF / IF / ELSE plan. Pure, no I/O: the caller supplies the ladder, the whole-book net GEX and flip,
and the daily bars. Display only. Never read by the guard or by sizing.

Ported from the author's own TraderMatrix builder (backend/src/services/share/tomorrowMap.ts), itself a port of mphinance's
tools/gamma_tomorrow.mjs. The rules below were each learned on a session where the tool was confidently wrong; do not quietly undo them:
  - Build levels from gamma that SURVIVES tonight (0DTE excluded; `survivor_ladder` does that).
  - A big negative strike below spot is a crowd (PUT WALL), not a floor.
  - Rallies stall into heavy long gamma (BRAKE) before the CEILING.
  - A short-gamma strike ABOVE spot is an ACCELERANT, not a wall.
  - Never assert structure from an absence of signal: a missing strike is "no listed strike", never a free SHELF; a thin book says so.
  - Scale to a realistic day: the yardstick is the median move from the PRIOR close, gap included.
  - UP chain: brake, ceiling, accelerant, the flip, "higher". DOWN chain: shelf, thin cushion, put wall, floor(spot).
  - The ELSE band is the GAP between the two IF triggers, so the three roads are exhaustive and the band always contains spot.
Text is derived (strikes, role names, sentences); per-strike open interest, a vendor field, is never quoted.

Prices here are in the SOURCE space (SPY). `to_futures` maps every price into futures space with the desk's frozen basis; the sentences
are then regenerated from the converted numbers by `describe`, never regex-rewritten.

The JS arithmetic is mirrored on purpose (Math.round is half-up, toFixed, stable sorts) so the three captured books in
tests/fixtures/tomorrow reproduce the TypeScript builder's strings exactly. Bump VERSION with ANY change that moves what gets drawn."""
import calendar
import math
import time

VERSION = 1
EXPIRING_REGIME_LABEL_SHARE = 0.10
MIN_BOOK_GEX = 250_000
WALL_SHARE = 0.28
YARDSTICK_DAYS = 7


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _jsround(x):
    return math.floor(x + 0.5)                       # Math.round: halves go up, unlike Python's round


def _px(v):
    """'7.5', '765', '766.67': strikes print bare, everything else to 2dp."""
    if float(v).is_integer():
        return str(int(v))
    if float(v * 2).is_integer():
        return repr(float(v))
    return f"{v:.2f}"


def _snap(x, tick):
    """Round to the nearest `tick` (half up), as gex_futures does; no tick means no snapping."""
    if not tick:
        return x
    n = math.floor(x / tick + 0.5)
    dec = len(f"{tick:.8f}".rstrip("0").partition(".")[2])
    return round(n * tick, dec + 2)


def _day_ms(t):
    try:
        return calendar.timegm(time.strptime(str(t)[:10], "%Y-%m-%d")) * 1000
    except (ValueError, OverflowError):
        return None


def bars_from_history(hist):
    """Daily {t, h, l, c} from snapshot history [{snapshotTime, spotPrice}], oldest first: what the mjs measures from its 168 h feed."""
    days = {}
    for p in hist or []:
        st, sp = p.get("snapshotTime"), p.get("spotPrice")
        if isinstance(st, str) and _fin(sp):
            days.setdefault(st[:10], []).append(sp)
    return [{"t": d, "h": max(px), "l": min(px), "c": px[-1]} for d, px in days.items()]


def _median_excursion(recent, spot):
    ex = []
    for i in range(1, len(recent)):
        pc, h, l = recent[i - 1]["c"], recent[i]["h"], recent[i]["l"]
        if not all(_fin(v) and v > 0 for v in (pc, h, l)):
            continue
        ex.append(max(abs(h - pc), abs(l - pc)))
    if not ex:
        return spot * 0.006
    ex.sort()
    return ex[len(ex) // 2]


def measure_typical_move(bars, spot, sessions=10):
    """Median of each session's furthest excursion from the PRIOR close, over the last 7 calendar days of dated bars, else the last
    `sessions` bars. window says which; '7d' matches the mjs."""
    recent, window, dated = bars[-(sessions + 1):], "sessions", 0
    last_t = next((b["t"] for b in reversed(bars) if b.get("t")), None)
    if last_t:
        last_d = _day_ms(last_t)
        cut = None if last_d is None else last_d - YARDSTICK_DAYS * 86_400_000       # an unparseable date matches nothing, as NaN does in JS
        windowed = [b for b in bars if cut is not None and b.get("t") and (_day_ms(b["t"]) is not None) and _day_ms(b["t"]) > cut]
        dated = len(windowed)
        if len(windowed) >= 3:
            recent, window = windowed, "7d"
    return {"move": _median_excursion(recent, spot), "window": window, "dated": dated}


def typical_move(bars, spot, sessions=10):
    return measure_typical_move(bars, spot, sessions)["move"]


def survivor_ladder(matrix, today):
    """Each strike's net GEX summed over the expirations that outlive `today` (an ISO date). Returns (ladder, expiring_share)."""
    live = [e > today for e in matrix["expirations"]]
    tot = kept = 0.0
    ladder = []
    for r in matrix["rows"]:
        net, any_ = 0.0, False
        for i, v in enumerate(r["gex"]):
            if not _fin(v):
                continue
            tot += abs(v)                           # magnitudes per expiry, not |sum|: offsetting signs would let kept exceed the total
            if live[i]:
                kept += abs(v)
                net += v
                any_ = True
        if any_:
            ladder.append({"strike": r["strike"], "netGex": net})
    ladder.sort(key=lambda s: s["strike"])
    return ladder, (1 - kept / tot if tot > 0 else None)


def build_tomorrow_map(spot, flip, net_gex, ladder, bars, expiring_share, regime_counts_unlisted_expiry=False, ratio=1.0, tick=None):
    """The map as a JSON-ready dict, or None when there is nothing honest to say (no spot, no measured net GEX, fewer than 3 strikes).

    Structure is always computed in SOURCE space (SPY), exactly as the parity-tested rules do. `ratio` and `tick` only convert what is
    OUTPUT: every price and every number inside a sentence is mapped to futures space (price * ratio, snapped to `tick`), so the text is
    generated from the converted numbers, never rewritten afterwards. The defaults leave the source space untouched."""
    scaled = ratio != 1.0 or tick is not None
    cv = (lambda x: _snap(x * ratio, tick)) if scaled else (lambda x: x)
    P = lambda v: _px(cv(v))
    P2 = lambda v: f"{cv(v):.2f}"
    if not _fin(spot) or spot <= 0:
        return None
    if not _fin(net_gex):
        return None                                  # no measured net GEX means no measured regime: never default to "positive"
    flip = flip if _fin(flip) else None
    expected = typical_move(bars, spot)
    near_all = sorted(s["strike"] for s in ladder if abs(s["strike"] - spot) <= spot * 0.1)
    gaps = sorted(d for d in (b - a for a, b in zip(near_all, near_all[1:])) if d > 0)
    step = gaps[len(gaps) // 2] if gaps else spot * 0.005
    half = max(spot * 0.014, expected, step * 2)
    lo, hi = spot - half, spot + half
    lad = sorted((s for s in ladder if lo <= s["strike"] <= hi and _fin(s["netGex"])), key=lambda s: s["strike"])
    if len(lad) < 3:
        return None

    tail = lambda v: abs(v - spot) > expected
    max_abs = max(abs(s["netGex"]) for s in lad)
    thin = max_abs < MIN_BOOK_GEX
    WALL = max_abs * WALL_SHARE
    regime = "negative" if net_gex < 0 else "positive"
    flip_on_price = flip is not None and abs(flip - spot) < spot * 0.0004
    above = [s for s in lad if s["strike"] > spot]
    below = [s for s in lad if s["strike"] < spot]

    pin_win = max(spot * 0.002, step * 1.01)
    near = [s for s in lad if abs(s["strike"] - spot) <= pin_win]
    pin_lvl = sorted(near, key=lambda s: -abs(s["netGex"]))[0] if near else None
    pin = pin_lvl["strike"] if pin_lvl else None

    by_desc = lambda xs: sorted(xs, key=lambda s: -s["netGex"])
    by_asc = lambda xs: sorted(xs, key=lambda s: s["netGex"])
    gate = None if thin else next(iter(by_desc([s for s in above if s["netGex"] > WALL])), None)
    accel = None if thin else next(iter(by_asc([s for s in above if s["netGex"] < -WALL and (gate is None or s["strike"] < gate["strike"])])), None)
    brake = next(iter(by_desc([s for s in above if s["netGex"] > WALL and s["strike"] < gate["strike"]])), None) if gate else None
    floor = None if thin else next(iter(by_asc([s for s in below if s["netGex"] < -WALL])), None)

    def wp(price, tag, note, open_=False):
        w = {"price": cv(price), "tag": tag, "note": note, "tail": tail(price)}
        if open_:
            w["open"] = True
        return w

    flip_up = flip is not None and flip > spot and not flip_on_price
    round_step = lambda v: float(f"{v:.6f}")
    floor_step = lambda v: round_step(math.floor(v / step + 1e-9) * step)
    ceil_step = lambda v: round_step(math.ceil(v / step - 1e-9) * step)

    # ── UP ──
    up_pts = []
    if flip_up:
        up_pts.append(wp(flip, f"{P(flip)} FLIP", "dealers stop chasing, start braking"))
    if accel:
        up_pts.append(wp(accel["strike"], f"{P(accel['strike'])} ACCELERANT", "dealers short gamma, they buy into strength"))
    if brake:
        up_pts.append(wp(brake["strike"], f"{P(brake['strike'])} BRAKE", "dealers sell rips here, rallies stall"))
    if gate:
        up_pts.append(wp(gate["strike"], f"{P(gate['strike'])} CEILING", "the move dies here"))
    up_lead = brake or gate or accel
    up_trigger = up_lead["strike"] if up_lead else (float(f"{P2(flip)}") if flip_up else None)
    if brake:
        up_cond = f"up into {P(brake['strike'])}"
    elif gate:
        up_cond = f"up into {P(gate['strike'])}"
    elif accel:
        up_cond = f"up through {P(accel['strike'])}"
    elif up_trigger is not None:
        up_cond = f"up through the {P(up_trigger)} flip"
    else:
        up_cond = "higher"
    rips = lambda sep: f"rips through {P(accel['strike'])}{sep}" if accel else ""
    if thin:
        up_rule = "book too thin to map the upside."
    elif gate:
        pre = (f"under the flip ({P2(flip)}) dealers chase, so it can get there fast. " if flip_up
               else "already above the flip, so dealers are braking. " if flip is not None and flip <= spot else "")
        if brake:
            up_rule = pre + f"{rips(', then ')}stalls at {P(brake['strike'])}: it can wick over, it tends to close under. ceiling {P(gate['strike'])}."
        else:
            up_rule = pre + f"{rips('. ')}nothing gives until {P(gate['strike'])}, and it tends to close under that."
    else:
        up_rule = f"{rips('. ')}no long-gamma strike above spot, so nothing in the book brakes it" + (f"; over {P2(flip)} dealers stop chasing" if flip_up else "") + "."

    # ── DOWN ── walk down from spot the way price would; contiguity matters (an "empty" band must not run over a real strike)
    down_pts, shelf, cushion = [], None, None
    if thin:
        down_rule = "book too thin to map the downside."
    else:
        walk = list(reversed(below))                                          # nearest first
        first = next((s for s in walk if abs(s["netGex"]) >= WALL), None)
        shelf = first if first and (not floor or first["strike"] != floor["strike"]) else None
        below_pin = lambda k: pin is None or pin >= spot or k < pin           # the pin is never air
        band_top = shelf["strike"] if shelf else math.inf
        between = [s for s in walk if s["strike"] < band_top and s["strike"] > floor["strike"] and below_pin(s["strike"])] if floor else []
        pin_under = pin_lvl if (pin_lvl and shelf and floor and floor["strike"] < pin_lvl["strike"] < shelf["strike"]) else None
        cushion = next(iter(sorted((s for s in between if s["netGex"] >= WALL * 0.1), key=lambda s: -s["netGex"])), None)
        pocket = ({"lo": min(s["strike"] for s in between), "hi": max(s["strike"] for s in between)}
                  if floor and len(between) >= 2 and all(abs(s["netGex"]) < WALL for s in between) else None)
        if shelf:
            down_pts.append(wp(shelf["strike"], f"{P(shelf['strike'])} LAST SHELF", "the last thing to lean on") if shelf["netGex"] >= 0
                            else wp(shelf["strike"], f"{P(shelf['strike'])} TRAPDOOR", "dealers short gamma, they sell into it"))
        if cushion:
            down_pts.append(wp(cushion["strike"], f"{P(cushion['strike'])} THIN CUSHION", "the only dealer buying down here, and it is small", True))
        elif pocket:
            down_pts.append(wp((pocket["lo"] + pocket["hi"]) / 2, "EMPTY", f"{P(pocket['lo'])} to {P(pocket['hi'])}, nothing here", True))
        if floor:
            down_pts.append(wp(floor["strike"], f"{P(floor['strike'])} PUT WALL", "a crowd, not a floor. the fight is here"))
        if shelf:
            lead = ((f"leans on {P(shelf['strike'])}. lose it and " if shelf["netGex"] >= 0 else f"trapdoor at {P(shelf['strike'])}. through it, ")
                    + (f"the {P(pin_under['strike'])} pin, then " if pin_under else ""))
            if not floor:
                down_rule = (f"leans on {P(shelf['strike'])}. " if shelf["netGex"] >= 0 else f"trapdoor at {P(shelf['strike'])}. ") + "no readable structure below it."
            elif cushion:
                down_rule = f"{lead}thin air. only real bid is {P(cushion['strike'])}. then {P(floor['strike'])}."
            elif pocket:
                down_rule = f"{lead}open air. {P(pocket['lo'])} to {P(pocket['hi'])} is empty. next stop {P(floor['strike'])}."
            else:
                down_rule = f"{lead}no thin band above {P(floor['strike'])}. grind, not a drop."
        elif cushion and floor:
            down_rule = f"that bid is thin, the only dealer buying above {P(floor['strike'])}. under it the {P(floor['strike'])} put wall, which is a crowd, not a floor."
        elif floor:
            down_rule = (f"open air above it: {P(pocket['lo'])} to {P(pocket['hi'])} is empty. the put wall is a crowd, not a floor." if pocket
                         else "the put wall is a crowd, not a floor, and there is nothing readable under it.")
        else:
            down_rule = "no readable structure below spot."
    down_lead = shelf or cushion or floor
    down_trigger = down_lead["strike"] if down_lead else floor_step(spot)
    down_cond = f"below {P(down_trigger)}"

    # ── RANGE (the ELSE) ── the gap between the two IF triggers
    range_hi = max(up_trigger if up_trigger is not None else ceil_step(max(pin if pin is not None else spot, flip if flip is not None else spot)),
                   ceil_step(spot))
    range_lo = min(down_trigger, floor_step(spot))
    pin_note = ("no listed strike here, regime unknown" if pin_lvl is None
                else "short gamma, so it chops hard" if pin_lvl["netGex"] < 0 else "long gamma, so it settles")
    range_pts = [wp(pin, f"{P(pin)} PIN", pin_note)] if pin is not None else []
    range_rule = ("no pin near price." if pin is None
                  else f"chop around the {P(pin)} pin. it knifes both edges." if regime == "negative"
                  else f"quiet drift into the {P(pin)} pin.")

    shown = None if not _fin(expiring_share) else _jsround(expiring_share * 100) / 100
    return {
        "version": VERSION, "regime": regime, "spot": cv(spot), "flip": cv(flip) if flip is not None else None, "pin": cv(pin) if pin is not None else None,
        "typical_move": _jsround(expected * ratio * 100) / 100, "expiring_share": shown,
        "regime_includes_expiring": regime_counts_unlisted_expiry is True or (shown is not None and shown > EXPIRING_REGIME_LABEL_SHARE),
        "thin_book": thin, "flip_on_price": flip_on_price,
        "roads": [
            {"key": "UP", "cond": up_cond, "rule": up_rule, "waypoints": up_pts},
            {"key": "DOWN", "cond": down_cond, "rule": down_rule, "waypoints": down_pts},
            {"key": "RANGE", "cond": f"between {P(range_lo)} and {P(range_hi)}", "rule": range_rule, "waypoints": range_pts,
             "band": {"lo": cv(range_lo), "hi": cv(range_hi)}},
        ],
    }


def branches(m):
    """The IF / IF / ELSE plan as (kind, condition sentence, rule) rows, in the order the card prints them."""
    r = {x["key"]: x for x in m["roads"]}
    return [("IF", f"it goes {r['UP']['cond']}", r["UP"]["rule"]), ("IF", f"it goes {r['DOWN']['cond']}", r["DOWN"]["rule"]),
            ("ELSE", f"it holds {r['RANGE']['cond']}", r["RANGE"]["rule"])]
