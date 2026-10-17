"""Reference levels and how price has behaved at them: EMA stack (8/21/34), ATR series, bounce statistics.

Pure functions, stdlib only, no I/O. The question this answers: "has price actually been
bouncing off this level, or does it just look like it should?" A level is only worth drawing if you can
say how many times it was tested and what happened, with the sample size beside the answer.

Definitions (all hypotheses to be judged by their sample sizes, not trusted):
  touch   a bar whose range comes within `tol_atr` x ATR of the level. Touches within `cluster` bars of
          the previous one are one event (price grinding on a level is not five tests).
  BOUNCE  within `look` bars price moved >= `away_atr` x ATR away from the level on the side it came
          from, before any close more than `fail_atr` x ATR through it.
  FAIL    a close more than `fail_atr` x ATR through the level first.
  NEUTRAL neither happened within `look` bars. PENDING: fewer than `look` bars of future exist yet;
          pending events are shown but never counted in the rate.
"""
import math
from typing import Optional, Sequence


def ema_series(values: Sequence[float], n: int) -> list:
    """EMA seeded with the SMA of the first n values (the usual charting convention). None before the seed."""
    if n < 1:
        raise ValueError("n must be >= 1")
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def atr_series(highs, lows, closes, n: int = 14) -> list:
    """Wilder ATR for every bar (None until n+1 bars exist)."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    a = sum(trs[:n]) / n
    out[n] = a
    for i in range(n + 1, len(closes)):
        a = (a * (n - 1) + trs[i - 1]) / n
        out[i] = a
    return out


def percentile_rank(x: Optional[float], series: Sequence[Optional[float]]) -> Optional[float]:
    """Percent of the series at or below x (0..100). None if x or the series is unusable."""
    vals = [v for v in series if v is not None]
    if x is None or len(vals) < 20:
        return None
    return round(100.0 * sum(1 for v in vals if v <= x) / len(vals), 1)


def ema_stack(closes: Sequence[float], periods=(8, 21, 34)) -> dict:
    """Last value of each EMA plus the stack state: BULL (fast>mid>slow, slow rising), BEAR, or MIXED."""
    series = {p: ema_series(closes, p) for p in periods}
    last = {p: s[-1] for p, s in series.items()}
    if any(v is None for v in last.values()):
        return {"state": "NA", "values": last, "series": series}
    f, m, s_ = (last[p] for p in periods)
    slow = series[periods[-1]]
    rising = slow[-1] is not None and slow[-2] is not None and slow[-1] > slow[-2]
    falling = slow[-1] is not None and slow[-2] is not None and slow[-1] < slow[-2]
    state = "BULL" if f > m > s_ and rising else "BEAR" if f < m < s_ and falling else "MIXED"
    return {"state": state, "values": last, "series": series}


def bounce_stats(bars: Sequence[dict], level: Sequence[Optional[float]], atr: Sequence[Optional[float]], *,
                 tol_atr: float = 0.15, away_atr: float = 0.75, fail_atr: float = 0.25, look: int = 6,
                 cluster: int = 3, max_events: int = 12) -> dict:
    """Count how price reacted each time it tested `level`. bars are dicts with o,h,l,c and t (optional)."""
    n = len(bars)
    events, last_touch = [], -10 ** 9
    for i in range(1, n):
        L, A = level[i], atr[i]
        if L is None or A is None or A <= 0:
            continue
        lo, hi = bars[i]["l"], bars[i]["h"]
        if not (lo <= L + tol_atr * A and hi >= L - tol_atr * A):
            continue
        if i - last_touch <= cluster:
            last_touch = i
            continue
        last_touch = i
        from_above = bars[i - 1]["c"] >= L          # came from above: this is a support test
        res = "PENDING"
        horizon = min(n - 1, i + look)
        for j in range(i + 1, horizon + 1):
            Lj, Aj = level[j], atr[j]
            if Lj is None or Aj is None:
                break
            c, h, l = bars[j]["c"], bars[j]["h"], bars[j]["l"]
            if from_above:
                if c < Lj - fail_atr * Aj:
                    res = "FAIL"
                    break
                if h >= Lj + away_atr * Aj:
                    res = "BOUNCE"
                    break
            else:
                if c > Lj + fail_atr * Aj:
                    res = "FAIL"
                    break
                if l <= Lj - away_atr * Aj:
                    res = "BOUNCE"
                    break
        else:
            if i + look <= n - 1:
                res = "NEUTRAL"
        events.append({"i": i, "t": bars[i].get("t"), "price": round(L, 4), "kind": "SUPPORT" if from_above else "RESISTANCE",
                       "result": res})
    resolved = [e for e in events if e["result"] != "PENDING"]
    b = sum(1 for e in resolved if e["result"] == "BOUNCE")
    f = sum(1 for e in resolved if e["result"] == "FAIL")
    z = sum(1 for e in resolved if e["result"] == "NEUTRAL")
    last = events[-1] if events else None
    return {"n": len(resolved), "bounced": b, "failed": f, "neutral": z,
            "rate": round(100.0 * b / len(resolved), 1) if resolved else None,
            "pending": len(events) - len(resolved),
            "last_result": last["result"] if last else None, "last_bars_ago": (n - 1 - last["i"]) if last else None,
            "events": events[-max_events:]}


PLACEBO_OFFSETS = (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5)   # in ATRs, per bar: control lines that are NOT a real level


def placebo_rate(bars, level, atr, offsets=PLACEBO_OFFSETS, **kw) -> dict:
    """How often price 'bounces' off a RANDOM line near it, measured with the identical rules.

    A level's hold rate means nothing alone: if price bounces off arbitrary lines 60% of the time, a level that
    'holds 70%' has shown almost nothing. Each control line is the real level shifted by a fixed number of ATRs
    on every bar, so it sits in the same neighbourhood and gets tested at a comparable frequency.
    """
    bounced = n = 0
    for k in offsets:
        shifted = [None if (lv is None or a is None) else lv + k * a for lv, a in zip(level, atr)]
        st = bounce_stats(bars, shifted, atr, **kw)
        bounced += st["bounced"]
        n += st["n"]
    return {"n": n, "bounced": bounced, "rate": round(100.0 * bounced / n, 1) if n else None}


MIN_TESTS = 30   # resolved tests before a verdict says anything positive. At 8 (the old value) a 15-point gap was 0.87 standard
                 # errors at p=0.6, so about one candidate in five cleared it on pure noise; at 30 the same gap is 1.7 SE.


def verdict(real: dict, placebo: dict, min_n: int = MIN_TESTS, margin: float = 15.0) -> str:
    """Plain-language read of real vs placebo. Conservative: needs both enough tests and a clear gap.

    This is the EFFECT-SIZE gate only (is the gap big enough to matter, on enough tests). It is not a significance test:
    `binom_sf` gives the p-value and `benjamini_hochberg` the multiple-comparison cut; projectx_desk/respect.py requires
    all three before it calls a level respected."""
    if real["n"] < min_n or real["rate"] is None or placebo["rate"] is None or placebo["n"] < min_n * 3:
        return "TOO_FEW_TESTS"
    gap = real["rate"] - placebo["rate"]
    if gap >= margin:
        return "ABOVE_RANDOM"
    if gap <= -margin:
        return "BELOW_RANDOM"
    return "NOT_DISTINGUISHABLE"


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p): the chance of AT LEAST k bounces in n tests if the true rate were the placebo's.

    One-sided, exact, stdlib only (log-space terms so n in the hundreds cannot underflow). p is clamped to [0, 1] because the
    calibrated placebo rate can be pushed past 100 by the surrogate correction. k <= 0 or n <= 0 is 1.0 (no evidence)."""
    if n <= 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p = min(1.0, max(0.0, float(p)))
    if p >= 1.0:
        return 1.0
    if p <= 0.0:
        return 0.0
    lp, lq, lgn = math.log(p), math.log1p(-p), math.lgamma(n + 1)
    tot = 0.0
    for i in range(k, n + 1):
        tot += math.exp(lgn - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return min(1.0, max(0.0, tot))


def benjamini_hochberg(pvals: Sequence[Optional[float]], q: float = 0.10) -> list:
    """Which hypotheses survive at false-discovery rate q (Benjamini and Hochberg 1995), aligned to the input.

    Testing ~25 candidate lines on 5 timeframes is 125 hypotheses; without this, a 5% test hands out about six false
    "respected" badges per scan with no effect present. A None or non-finite p-value is not a hypothesis: it stays None
    and never passes. Returns True/False/None per entry."""
    idx = [i for i, p in enumerate(pvals) if isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p)]
    out: list = [None] * len(pvals)
    m = len(idx)
    if m == 0 or not (0.0 < q < 1.0):
        return out
    order = sorted(idx, key=lambda i: pvals[i])
    cut = None
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            cut = pvals[i]
    for i in idx:
        out[i] = cut is not None and pvals[i] <= cut
    return out
