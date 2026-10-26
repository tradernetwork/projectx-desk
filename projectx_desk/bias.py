"""Advisory trend context with a letter. Pure, no I/O. Display only: no guard check and no sizing input reads it.

NOT a signal. A 21-day backtest of the first version (docs/DECISIONS.md, Round 17) found no follow-through edge, and this version
has NOT been backtested. It is hand-set, not fitted, and says what it is looking at (rule version RULE below):
  direction  sign of the votes: 5m / 15m / 1h EMA stack, price vs VWAP, tape delta (needs >= 3 reads, else NA)
  points     stacks 0.5 each + VWAP 0.5 + tape 0.5 + ENTRY LOCATION up to 1.0, so MAX_SCORE is 3.5. A missing read earns nothing (fixed
             denominator). Entry: within 0.75 ATR of the 21 EMA = PULLBACK (1.0); then it fades linearly to 0 at 2 ATR beyond it (EXTENDED,
             chasing); through the EMA against the bias = THROUGH_EMA (0)
  penalties  graded, never cliffs: volatility (ATR percentile 75 to 95 costs up to 0.75), RSI stretched against the move (30 down to 20 costs
             up to 0.75), ADX under 20 (costs up to 1.0 as it falls to 10)
  caps       qualitative states only, the letter can never beat: B when a read DISSENTS; C when EXTENDED, THROUGH_EMA, or inside the first
             15 minutes after the 08:30 CT open
  flags      DISSENT, VOL_HIGH, VOL_EXTREME, STRETCHED, EXTENDED_RSI, RANGING, FLOW_NA and the caps, so the letter always says why
Letters: A >= 3.0, B >= 2.25, C >= 1.5, else D ("-" = no lean). The trader decides; this is context."""
import math

RULE = "v2.2"
MIN_READS = 3
MAX_SCORE = 3.5
W_STACK, W_VWAP, W_FLOW, W_ENTRY = 0.5, 0.5, 0.5, 1.0
PULLBACK_ATR, EXTEND_ATR = 0.75, 2.0
OPEN_M, OPEN_CHOP_MIN = 8 * 60 + 30, 15
BANDS = ((3.0, "A"), (2.25, "B"), (1.5, "C"), (0.0, "D"))
ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}
CAUTION = frozenset({"VOL_EXTREME", "EXTENDED", "STRETCHED", "EXTENDED_RSI", "OPEN_CHOP", "THROUGH_EMA", "RANGING", "DISSENT", "EMA_TURN"})
FLOW_MIN_SKEW = 0.05


def _ramp(x, lo, hi):
    """0 at or below lo, 1 at or above hi, linear between."""
    return 0.0 if x <= lo else 1.0 if x >= hi else (x - lo) / (hi - lo)


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
    return "D"


def _cap(letter, cap):
    return cap if ORDER[cap] > ORDER[letter] else letter


def bias_view(last, vwap, ema21, atr, atr_pctile, adx, stacks, flow, rsi=None, ct_minute=None, adx_chop=20.0, slope21=None):
    """stacks: {"5m": "BULL"|"BEAR"|"MIXED"|"NA", "15m": ..., "1h": ...}. rsi: 5m RSI(14). ct_minute: minutes since midnight CT.
    slope21: "UP"|"DOWN"|"FLAT"|None, the 5m EMA21 slope on the closed bars (regime.ema_slope). Returns enums and numbers only."""
    last, vwap, ema21, atr, rsi = _num(last), _num(vwap), _num(ema21), _num(atr), _num(rsi)
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
    out = {"rule": RULE, "direction": "NA", "grade": "NA", "score": None, "max": MAX_SCORE, "agree": 0, "reads": len(reads),
           "votes": reads, "flags": [], "entry": None, "caution": False}
    if len(reads) < MIN_READS:
        return out
    net = sum(reads.values())
    direction = "LONG" if net > 0 else "SHORT" if net < 0 else "NEUTRAL"
    out["direction"] = direction
    if direction == "NEUTRAL":
        out["grade"], out["score"] = "-", 0.0
        out["flags"].append("NO_LEAN")
        return out
    sgn = 1 if direction == "LONG" else -1
    flags = out["flags"]
    out["agree"] = sum(1 for v in reads.values() if v == sgn)
    score = sum(W_STACK for tf in ("5m", "15m", "1h") if reads.get(tf) == sgn)
    score += W_VWAP if reads.get("vwap") == sgn else 0.0
    if "flow" in reads:
        score += W_FLOW if reads["flow"] == sgn else 0.0
    else:
        flags.append("FLOW_NA")
    cap = "A"
    if any(v == -sgn for v in reads.values()):
        flags.append("DISSENT")
        cap = _cap(cap, "B")
    if (sgn > 0 and slope21 == "DOWN") or (sgn < 0 and slope21 == "UP"):   # v2.2: the 21 EMA is turning against the lean. Hand-set cap, not fitted
        flags.append("EMA_TURN")
        cap = _cap(cap, "B")
    d = sgn * (last - ema21) / atr if (last is not None and ema21 is not None and atr and atr > 0) else None
    if d is None:
        flags.append("ENTRY_NA")
    elif d < -PULLBACK_ATR:
        out["entry"] = "THROUGH_EMA"
        flags.append("THROUGH_EMA")
        cap = _cap(cap, "C")
    else:
        score += W_ENTRY * (1.0 - _ramp(d, PULLBACK_ATR, EXTEND_ATR))
        out["entry"] = "PULLBACK" if d <= PULLBACK_ATR else "EXTENDED" if d > EXTEND_ATR else "MID"
        if out["entry"] == "EXTENDED":
            flags.append("EXTENDED")
            cap = _cap(cap, "C")
    if rsi is not None:
        against = rsi if sgn < 0 else 100.0 - rsi      # low = the move has run to an extreme in the bias direction
        if against < 30.0:
            flags.append("EXTENDED_RSI" if out["entry"] == "EXTENDED" else "STRETCHED")
            score -= 0.75 * _ramp(30.0 - against, 0.0, 10.0)
    a, p = _num(adx), _num(atr_pctile)
    if a is not None and a < adx_chop:
        flags.append("RANGING")
        score -= 1.0 * _ramp(adx_chop - a, 0.0, 10.0)
    if p is not None:
        score -= 0.75 * _ramp(p, 75.0, 95.0)
        if p > 90:
            flags.append("VOL_EXTREME")
        elif p > 75:
            flags.append("VOL_HIGH")
    if isinstance(ct_minute, (int, float)) and not isinstance(ct_minute, bool) and OPEN_M <= ct_minute < OPEN_M + OPEN_CHOP_MIN:
        flags.append("OPEN_CHOP")
        cap = _cap(cap, "C")
    score = max(0.0, min(MAX_SCORE, score))
    out["score"] = round(score, 2)
    out["grade"] = _cap(grade_for(score), cap)
    out["caution"] = any(f in CAUTION for f in flags)
    return out
