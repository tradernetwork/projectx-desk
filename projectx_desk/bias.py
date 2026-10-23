"""Advisory direction and grade. Pure, no I/O. Display only: no guard check and no sizing input reads it.

Direction is the sign of how many independent reads lean one way (5m / 15m / 1h EMA stack, price vs VWAP, tape delta).
Grade is 0..5, same A+..F bands as the Ghost indicator: agreement (0..3) + trend regime (0..1) + volatility headroom (0..1)
minus 0.5 when price is stretched far from the 21 EMA in the signal's direction (chasing). A read that cannot be made is
left out, never guessed; fewer than 3 reads is NA. This is context for the trader, not a signal and not an edge claim."""
import math

MIN_READS = 3
STRETCH_ATR = 3.0       # |last - EMA21| beyond this many ATR counts as extended
FLOW_MIN_SKEW = 0.05    # |delta| / (buy + sell) below this is no lean
BANDS = ((4.5, "A+"), (4.0, "A"), (3.0, "B"), (2.0, "C"), (1.0, "D"), (0.0, "F"))


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _stack_vote(state):
    return {"BULL": 1, "BEAR": -1, "MIXED": 0}.get(state)


def _flow_vote(flow):
    """Largest COMPLETE trailing window's skew. Incomplete windows are not used."""
    if not isinstance(flow, dict):
        return None
    best = None
    for w, d in (flow.get("w") or {}).items():
        if isinstance(d, dict) and d.get("complete") and (best is None or w > best[0]):
            best = (w, d)
    if best is None:
        return None
    b, s = _num(best[1].get("buy")), _num(best[1].get("sell"))
    if b is None or s is None or b + s <= 0:
        return None
    skew = (b - s) / (b + s)
    return 0 if abs(skew) < FLOW_MIN_SKEW else (1 if skew > 0 else -1)


def grade_for(score):
    for floor, g in BANDS:
        if score >= floor:
            return g
    return "F"


def bias_view(last, vwap, ema21, atr, atr_pctile, adx, stacks, flow, adx_chop=20.0):
    """stacks: {"5m": "BULL"|"BEAR"|"MIXED"|"NA", "15m": ..., "1h": ...}. Returns a dict of enums and numbers only."""
    last, vwap, ema21, atr = _num(last), _num(vwap), _num(ema21), _num(atr)
    reads = {}
    for tf in ("5m", "15m", "1h"):
        v = _stack_vote((stacks or {}).get(tf))
        if v is not None:
            reads[tf] = v
    if last is not None and vwap is not None and last != vwap:
        reads["vwap"] = 1 if last > vwap else -1
    fv = _flow_vote(flow)
    if fv is not None:
        reads["flow"] = fv
    base = {"direction": "NA", "grade": "NA", "score": None, "agree": 0, "reads": len(reads), "votes": reads, "flags": []}
    if len(reads) < MIN_READS:
        return base
    net = sum(reads.values())
    direction = "LONG" if net > 0 else "SHORT" if net < 0 else "NEUTRAL"
    base["direction"] = direction
    if direction == "NEUTRAL":
        base["grade"], base["score"] = "F", 0.0
        base["flags"].append("NO_LEAN")
        return base
    sgn = 1 if direction == "LONG" else -1
    agree = sum(1 for v in reads.values() if v == sgn)
    base["agree"] = agree
    score = 3.0 * agree / len(reads)
    a = _num(adx)
    if a is not None and a >= adx_chop:
        score += 1.0
    else:
        base["flags"].append("RANGING" if a is not None else "ADX_NA")
    cap = None
    p = _num(atr_pctile)
    if p is None:
        base["flags"].append("ATR_PCTILE_NA")
    elif p <= 75:
        score += 1.0
    elif p <= 90:
        score += 0.5
        base["flags"].append("VOL_HIGH")
    else:
        base["flags"].append("VOL_EXTREME")
        cap = 3.9      # a wide, fast tape never earns an A however well the reads agree
    if last is not None and ema21 is not None and atr and atr > 0 and sgn * (last - ema21) / atr > STRETCH_ATR:
        score -= 0.5
        base["flags"].append("EXTENDED")
    if any(v == -sgn for v in reads.values()):
        base["flags"].append("DISSENT")
    score = max(0.0, min(5.0 if cap is None else cap, score))
    base["score"] = round(score, 1)
    base["grade"] = grade_for(score)
    return base
