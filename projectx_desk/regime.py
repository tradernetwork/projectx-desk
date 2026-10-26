"""Chop and late-in-the-move measures. Pure, display and logging only: nothing here is read by the guard or by sizing.

ADX lags (Wilder 14 needs 29 bars: about 2.4 hours of 5m, a day and a half of 1h), so it says nothing at an open. Two session-local
measures sit beside it:
  efficiency   net move over the last n closes divided by the path travelled, 0 (pure chop) to 1 (a straight line)
  vwap_x       how many times the close crossed VWAP in the last n bars (a pinned, whipsawing market crosses a lot)
and one for the other failure the A-grade charts showed, entries at the end of a flush:
  run_extent   how far price has run off the n-bar low (run_up) and off the n-bar high (run_dn), in ATRs

static/regime.js is the same arithmetic for the mini charts; tests/test_regime.py pins the two together."""
import math

from projectx_desk import sizing

ER_N = 12
RUN_N = 24
CHOP_ER = 0.20            # below this the last hour went almost nowhere for the distance it travelled


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def efficiency(closes, n=ER_N):
    if len(closes) < n + 1 or not all(_fin(c) for c in closes[-(n + 1):]):
        return None
    seg = closes[-(n + 1):]
    path = sum(abs(seg[i + 1] - seg[i]) for i in range(n))
    return None if path <= 0 else abs(seg[-1] - seg[0]) / path


def vwap_cross_count(closes, vwaps, n=ER_N):
    pts = [(c, v) for c, v in zip(closes[-(n + 1):], vwaps[-(n + 1):]) if _fin(c) and _fin(v)]
    side = [1 if c > v else -1 if c < v else 0 for c, v in pts]
    side = [s for s in side if s]
    return sum(1 for a, b in zip(side, side[1:]) if a != b) if len(side) >= 2 else None


def run_extent(highs, lows, closes, atr, n=RUN_N):
    """(run_up, run_dn) in ATRs. Needs a full window, so a young session reads None rather than a flattering small number."""
    if not _fin(atr) or atr <= 0 or len(closes) < n or not all(_fin(x) for x in (*highs[-n:], *lows[-n:], closes[-1])):
        return None, None
    return (closes[-1] - min(lows[-n:])) / atr, (max(highs[-n:]) - closes[-1]) / atr


def tf_regime(rows):
    """ADX14 and efficiency from bar rows ({high, low, close}, oldest first, CLOSED bars). state is UNKNOWN until ADX is defined."""
    hi, lo, cl = ([r[k] for r in rows] for k in ("high", "low", "close"))
    a = None
    if len(cl) >= 29 and all(_fin(x) for x in (*hi, *lo, *cl)):
        res = sizing.adx(hi, lo, cl, 14)
        a = res[0] if res else None
    er = efficiency(cl)
    state = "UNKNOWN" if a is None else "RANGING" if a < sizing.ADX_CHOP else "TRENDING"
    return {"adx": a, "er": er, "state": state, "choppy": None if er is None else er < CHOP_ER}


def r2(v, nd=2):
    return round(v, nd) if _fin(v) else None
