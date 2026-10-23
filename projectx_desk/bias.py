"""Advisory trend context with a letter. Pure, no I/O. Display only: no guard check and no sizing input reads it.

NOT a signal. A 21-day backtest (docs/DECISIONS.md, Round 17) found the earlier version did not rank outcomes, so v2 is hand-set,
not fitted, and says what it is looking at:
  direction  sign of the votes: 5m / 15m / 1h EMA stack, price vs VWAP, tape delta (needs >= 3 reads, else NA)
  points     stacks 0.5 each (max 1.5) + VWAP 0.5 + tape 0.5 + ENTRY LOCATION up to 2.0 (a missing read earns nothing: fixed denominator)
  entry      price within 0.75 ATR of the 21 EMA = PULLBACK (2.0); 0.75..2 ATR beyond it = MID (1.0); beyond 2 ATR = EXTENDED (0, chasing);
             through the EMA against the bias = THROUGH_EMA (0, the trend is already broken)
  caps       the letter can never beat: C when EXTENDED, THROUGH_EMA, STRETCHED (RSI against the move), RANGING (ADX < 20) or inside the
             first 15 minutes of the 08:30 CT open; D when REVERSAL_RISK (extended and RSI stretched); B when ATR is above the 90th percentile
  flags      DISSENT, VOL_HIGH, FLOW_NA and the caps above, so the letter always says why it is what it is
Letters: A >= 3.5, B >= 2.5, C >= 1.5, else D (F = no lean). The trader decides; this is context."""
import math

MIN_READS = 3
W_STACK, W_VWAP, W_FLOW = 0.5, 0.5, 0.5
PULLBACK_ATR, EXTEND_ATR = 0.75, 2.0
RSI_STRETCH, RSI_CLIMAX = 30.0, 25.0
OPEN_M, OPEN_CHOP_MIN = 8 * 60 + 30, 15
BANDS = ((3.5, "A"), (2.5, "B"), (1.5, "C"), (0.0, "D"))
ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}
CAUTION = frozenset({"VOL_EXTREME", "EXTENDED", "STRETCHED", "REVERSAL_RISK", "OPEN_CHOP", "THROUGH_EMA", "RANGING"})
FLOW_MIN_SKEW = 0.05


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


def bias_view(last, vwap, ema21, atr, atr_pctile, adx, stacks, flow, rsi=None, ct_minute=None, adx_chop=20.0):
    """stacks: {"5m": "BULL"|"BEAR"|"MIXED"|"NA", "15m": ..., "1h": ...}. rsi: 5m RSI(14). ct_minute: minutes since midnight CT.
    Returns enums and numbers only."""
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
    out = {"direction": "NA", "grade": "NA", "score": None, "agree": 0, "reads": len(reads), "votes": reads, "flags": [],
           "entry": None, "caution": False}
    if len(reads) < MIN_READS:
        return out
    net = sum(reads.values())
    direction = "LONG" if net > 0 else "SHORT" if net < 0 else "NEUTRAL"
    out["direction"] = direction
    if direction == "NEUTRAL":
        out["grade"], out["score"] = "F", 0.0
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
    if any(v == -sgn for v in reads.values()):
        flags.append("DISSENT")
    cap = "A"
    d = sgn * (last - ema21) / atr if (last is not None and ema21 is not None and atr and atr > 0) else None
    if d is None:
        flags.append("ENTRY_NA")
    elif d < -PULLBACK_ATR:
        out["entry"] = "THROUGH_EMA"
        flags.append("THROUGH_EMA")
        cap = _cap(cap, "C")
    elif d <= PULLBACK_ATR:
        out["entry"] = "PULLBACK"
        score += 2.0
    elif d <= EXTEND_ATR:
        out["entry"] = "MID"
        score += 1.0
    else:
        out["entry"] = "EXTENDED"
        flags.append("EXTENDED")
        cap = _cap(cap, "C")
    if rsi is not None:
        against = rsi if sgn < 0 else 100.0 - rsi      # low = the move has run to an extreme in the bias direction
        if against < RSI_CLIMAX and out["entry"] == "EXTENDED":
            flags.append("REVERSAL_RISK")
            cap = _cap(cap, "D")
        elif against < RSI_STRETCH:
            flags.append("STRETCHED")
            cap = _cap(cap, "C")
    a, p = _num(adx), _num(atr_pctile)
    if a is not None and a < adx_chop:
        flags.append("RANGING")
        cap = _cap(cap, "C")
    if p is not None and p > 90:
        flags.append("VOL_EXTREME")
        cap = _cap(cap, "B")
    elif p is not None and p > 75:
        flags.append("VOL_HIGH")
    if isinstance(ct_minute, (int, float)) and not isinstance(ct_minute, bool) and OPEN_M <= ct_minute < OPEN_M + OPEN_CHOP_MIN:
        flags.append("OPEN_CHOP")
        cap = _cap(cap, "C")
    out["score"] = round(max(0.0, min(5.0, score)), 1)
    out["grade"] = _cap(grade_for(score), cap)
    out["caution"] = any(f in CAUTION for f in flags)
    return out
