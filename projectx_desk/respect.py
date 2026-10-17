"""Which reference levels is price actually respecting, per timeframe? Pure functions, stdlib only, no I/O.

Runs the machinery in px_levels (bounce_stats, the random-line placebo, verdict) over a fixed family of candidate
level series on each timeframe and ranks them by edge over the placebo. This is DESCRIPTIVE HISTORY, never a forecast.

Honesty rules (Round 10 tightened the first three; docs/DECISIONS.md has the arithmetic):
  * A level is RESPECTED only when THREE gates pass: (1) effect size: at least MIN_N resolved tests and a hold rate
    `margin` points above the calibrated placebo (px_levels.verdict); (2) significance: the one-sided exact binomial
    p-value of that many bounces against the placebo rate; (3) multiplicity: that p-value survives a Benjamini-Hochberg
    cut at FDR_Q over EVERY candidate on EVERY timeframe this scan tested (`family` in the output, about 125). A gap that
    clears the margin but not the FDR cut is reported NOT_DISTINGUISHABLE. Rows carry `p_value` and `fdr` so a reader
    can see which gate failed.
  * `tested` (per timeframe) and `family` (whole scan) are reported next to the winners: disclosure, with the correction
    above doing the actual work.
  * Each timeframe's events are split into an earlier and a later half. `stable` is True only if the level passes the
    effect-size gate (ABOVE_RANDOM at HALF_MIN_N tests) in BOTH halves, False if it fails either, None if either half
    has too few tests to judge. Stability is a robustness check, not a second significance test.
  * Callers should still badge SMALL SAMPLE when `tests` < 30; with MIN_N = 30 that now coincides with TOO_FEW_TESTS.
  * Self-referential levels (Bollinger/Keltner bands, tight EMAs) are tested at price extremes, so the random-line
    placebo is biased against them: on pure random walks bb_lo/bb_up show ~+20 points of fake edge. Each candidate is
    therefore also scored on SURRG surrogates of the same bars built by a CIRCULAR BLOCK bootstrap of the bar shapes
    (blocks of BLOCK bars, so volatility clustering survives while any tie between a line and the bars after it is
    destroyed); any positive mean gap it shows there is ADDED to its placebo rate. This only ever raises the bar, so
    `placebo_rate` in the output is the calibrated baseline, not the raw px_levels number. SURRG is small because the
    scan runs live in pure Python; it calibrates the baseline, it is not a 1000-draw null distribution (see Round 10).
  * Not changed on purpose: BOUNCE is judged on a wick (0.75 ATR) and FAIL on a close (0.25 ATR). The placebo lines are
    scored with the identical rules at the same bars, so whatever that asymmetry does to the hold rate it does to the
    placebo rate too; the gap and the p-value compare like with like.
  * NO LOOKAHEAD: every level value at bar i uses bars <= i only (tests/test_px_respect.py proves it by truncation).
    Swing pivots are only used once `pivot_lookback` bars have confirmed them.
Bars may use the keys o,h,l,c,v,t (px_levels) or open,high,low,close,volume,timestamp (the /bars endpoint).
"""
import math
import random
from typing import Optional, Sequence

from projectx_desk import levels as L
from projectx_desk.tradeday import parse_ts, trade_date

TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
INTRADAY = ("5m", "15m", "1h", "4h")
SURRG = 3            # block-bootstrap surrogates used to calibrate the placebo bias per candidate
BLOCK = 20           # bars per bootstrap block: long enough to keep intraday volatility clustering, short enough to break level ties
MIN_BARS = 60
MIN_N = L.MIN_TESTS  # 30: minimum resolved tests for a headline verdict (was 8; see docs/DECISIONS.md Round 10)
HALF_MIN_N = 15      # per-half minimum for the stability check (half samples are smaller by construction)
FDR_Q = 0.10         # Benjamini-Hochberg false-discovery rate over every candidate x timeframe in one scan
NOTES = ("TOO_FEW_BARS",)
RESPECTED = "ABOVE_RANDOM"   # what px_levels.verdict returns when real beats placebo by the margin with enough tests

# fixed allowlist: id -> (family, label). Labels are never built from input.
CATALOG = {}
for _p in (8, 13, 21, 34, 55, 89, 200):
    CATALOG[f"ema{_p}"] = ("ema", f"EMA {_p}")
for _p in (20, 50, 200):
    CATALOG[f"sma{_p}"] = ("sma", f"SMA {_p}")
CATALOG.update({
    "kc_up15": ("keltner", "Keltner upper 1.5"), "kc_lo15": ("keltner", "Keltner lower 1.5"),
    "kc_up20": ("keltner", "Keltner upper 2.0"), "kc_lo20": ("keltner", "Keltner lower 2.0"),
    "kc_mid": ("keltner", "Keltner mid"),
    "bb_up": ("boll", "Bollinger upper"), "bb_lo": ("boll", "Bollinger lower"), "bb_mid": ("boll", "Bollinger mid"),
    "vwap": ("vwap", "Session VWAP"),
    "pdh": ("prior", "Prior high"), "pdl": ("prior", "Prior low"), "pdc": ("prior", "Prior close"),
    "fib382": ("fib", "Fib 0.382"), "fib50": ("fib", "Fib 0.5"), "fib618": ("fib", "Fib 0.618"),
})


def _num(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ts_ms(x) -> Optional[int]:
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        f = _num(x)
        if f is None or f <= 0:
            return None
        return int(f * 1000) if f < 1e11 else int(f)
    try:
        return int(parse_ts(x).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _first(b, *keys):
    for k in keys:
        if k in b:
            return b[k]
    return None


def normalize(bars) -> list:
    """Clean bars: finite OHLC with h >= l, ascending in time. Bad bars are dropped, never guessed."""
    out = []
    if not isinstance(bars, (list, tuple)):
        return out
    for b in bars:
        if not isinstance(b, dict):
            continue
        o, h, l, c = (_num(_first(b, *k)) for k in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")))
        if None in (o, h, l, c) or h < l:
            continue
        v = _num(_first(b, "v", "volume"))
        out.append({"o": o, "h": h, "l": l, "c": c, "v": v if v is not None and v >= 0 else 0.0,
                    "t": _ts_ms(_first(b, "t", "timestamp"))})
    if len(out) > 1 and all(b["t"] is not None for b in out):
        out.sort(key=lambda b: b["t"])
    return out


# ---------------------------------------------------------------------------------------------- level builders
# Every builder returns a list the length of the bars, None where undefined, value at i from bars <= i only.

def sma_series(values: Sequence[float], n: int) -> list:
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def boll_series(closes: Sequence[float], n: int = 20, k: float = 2.0):
    """(upper, lower, mid) with population stdev over the trailing n closes."""
    mid = sma_series(closes, n)
    up, lo = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
    return up, lo, mid


def keltner_series(closes, atr10, n: int = 20):
    """dict of mid/up15/lo15/up20/lo20: EMA(n) +/- mult x ATR10."""
    mid = L.ema_series(closes, n)
    res = {"mid": mid}
    for name, k in (("15", 1.5), ("20", 2.0)):
        res["up" + name] = [None if (m is None or a is None) else m + k * a for m, a in zip(mid, atr10)]
        res["lo" + name] = [None if (m is None or a is None) else m - k * a for m, a in zip(mid, atr10)]
    return res


def vwap_series(bars) -> Optional[list]:
    """Session VWAP (typical price x volume), reset at the 17:00 CT trading-day roll. None if unusable."""
    ts = [b["t"] for b in bars]
    if sum(1 for t in ts if t is not None) < 0.9 * len(bars) or sum(b["v"] for b in bars) <= 0:
        return None
    out, day, pv, vol = [None] * len(bars), None, 0.0, 0.0
    from datetime import datetime, timezone
    for i, b in enumerate(bars):
        if b["t"] is None:
            continue
        d = trade_date(datetime.fromtimestamp(b["t"] / 1000, timezone.utc))
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
        vol += b["v"]
        if vol > 0:
            out[i] = pv / vol
    return out


def prior_series(bars, weekly: bool):
    """(prior high, prior low, prior close) of the previous COMPLETE period (trade day, or ISO week if weekly)."""
    n = len(bars)
    ph, pl, pc = [None] * n, [None] * n, [None] * n
    if sum(1 for b in bars if b["t"] is not None) < 0.9 * n:
        return None
    from datetime import datetime, timezone
    key = None
    cur = None           # [hi, lo, close] of the period in progress
    prev = None
    for i, b in enumerate(bars):
        if b["t"] is None:
            if prev:
                ph[i], pl[i], pc[i] = prev
            continue
        d = trade_date(datetime.fromtimestamp(b["t"] / 1000, timezone.utc))
        k = d.isocalendar()[:2] if weekly else d
        if k != key:
            if cur is not None:
                prev = tuple(cur)
            key, cur = k, [b["h"], b["l"], b["c"]]
        else:
            cur[0], cur[1], cur[2] = max(cur[0], b["h"]), min(cur[1], b["l"]), b["c"]
        if prev:
            ph[i], pl[i], pc[i] = prev
    return ph, pl, pc


def fib_series(bars, lookback: int = 5, ratios=(0.382, 0.5, 0.618)) -> dict:
    """Retracements of the latest confirmed swing. A pivot at bar j needs bars j-k..j+k, so it is only used from bar
    j+k onward (no lookahead). The swing is last pivot high vs last pivot low; the retracement is measured back from
    whichever of the two came later."""
    n, k = len(bars), lookback
    out = {r: [None] * n for r in ratios}
    hi = lo = None       # (index, price)
    for i in range(n):
        j = i - k
        if j >= k:
            wh = [bars[x]["h"] for x in range(j - k, i + 1)]
            wl = [bars[x]["l"] for x in range(j - k, i + 1)]
            if bars[j]["h"] == max(wh) and wh.count(bars[j]["h"]) == 1:
                hi = (j, bars[j]["h"])
            if bars[j]["l"] == min(wl) and wl.count(bars[j]["l"]) == 1:
                lo = (j, bars[j]["l"])
        if hi and lo and hi[1] > lo[1]:
            rng = hi[1] - lo[1]
            for r in ratios:
                out[r][i] = hi[1] - r * rng if hi[0] > lo[0] else lo[1] + r * rng
    return out


def build_levels(bars, tf: str, pivot_lookback: int = 5) -> dict:
    """id -> level series for every candidate that can exist on these bars. Pure function of the bars."""
    closes = [b["c"] for b in bars]
    highs, lows = [b["h"] for b in bars], [b["l"] for b in bars]
    lv = {}
    for p in (8, 13, 21, 34, 55, 89, 200):
        lv[f"ema{p}"] = L.ema_series(closes, p)
    for p in (20, 50, 200):
        lv[f"sma{p}"] = sma_series(closes, p)
    kc = keltner_series(closes, L.atr_series(highs, lows, closes, 10))
    lv.update({"kc_up15": kc["up15"], "kc_lo15": kc["lo15"], "kc_up20": kc["up20"], "kc_lo20": kc["lo20"],
               "kc_mid": kc["mid"]})
    up, lo, mid = boll_series(closes)
    lv.update({"bb_up": up, "bb_lo": lo, "bb_mid": mid})
    if tf in INTRADAY:
        vw = vwap_series(bars)
        if vw is not None:
            lv["vwap"] = vw
    pr = prior_series(bars, weekly=(tf == "1d"))
    if pr is not None:
        lv["pdh"], lv["pdl"], lv["pdc"] = pr
    fb = fib_series(bars, pivot_lookback)
    lv["fib382"], lv["fib50"], lv["fib618"] = fb[0.382], fb[0.5], fb[0.618]
    return {k: v for k, v in lv.items() if any(x is not None for x in v)}


# ---------------------------------------------------------------------------------------------- scoring

def _tally(events) -> dict:
    res = [e["result"] for e in events if e["result"] != "PENDING"]
    b, f, z = res.count("BOUNCE"), res.count("FAIL"), res.count("NEUTRAL")
    n = len(res)
    return {"n": n, "bounced": b, "failed": f, "neutral": z, "rate": round(100.0 * b / n, 1) if n else None}


def _score(bars, level, atr, mid: int, halves: bool = True):
    """Full-sample and half-sample (real, placebo) tallies from ONE pass of px_levels.bounce_stats per line."""
    kw = {"max_events": 10 ** 9}
    real_ev = L.bounce_stats(bars, level, atr, **kw)["events"]
    plc_ev = []
    for k in L.PLACEBO_OFFSETS:
        shifted = [None if (lv is None or a is None) else lv + k * a for lv, a in zip(level, atr)]
        plc_ev.extend(L.bounce_stats(bars, shifted, atr, **kw)["events"])
    parts = {}
    sels = (("all", lambda e: True), ("a", lambda e: e["i"] < mid), ("b", lambda e: e["i"] >= mid))
    for name, sel in (sels if halves else sels[:1]):
        parts[name] = (_tally([e for e in real_ev if sel(e)]), _tally([e for e in plc_ev if sel(e)]))
    return parts


def _shift(plc: dict, d: float) -> dict:
    if d <= 0 or plc["rate"] is None:
        return plc
    return {**plc, "rate": plc["rate"] + d}


def surrogate(bars, seed: int, block: int = BLOCK) -> list:
    """Same bars with the bar-to-bar shapes (o/h/l/c offsets from the prior close) resampled by a circular block
    bootstrap: runs of `block` consecutive shapes are drawn at random start offsets (wrapping at the end) and laid end to
    end until the series is full. Short-range dependence (volatility clustering) survives; the tie between a candidate
    line and the bars that follow it does not. Timestamps and volumes keep their positions. Deterministic per seed."""
    shapes = [(b["o"] - a["c"], b["h"] - a["c"], b["l"] - a["c"], b["c"] - a["c"]) for a, b in zip(bars, bars[1:])]
    m = len(shapes)
    rng = random.Random(seed)
    k = max(1, min(int(block), m)) if m else 1
    draw = []
    while len(draw) < m:
        start = rng.randrange(m)
        draw.extend(shapes[(start + j) % m] for j in range(k))
    out, pc = [dict(bars[0])], bars[0]["c"]
    for b, (o, h, l, c) in zip(bars[1:], draw):
        out.append({"o": pc + o, "h": pc + h, "l": pc + l, "c": pc + c, "v": b["v"], "t": b["t"]})
        pc += c
    return out


def _null_gaps(bars, tf: str, pivot_lookback: int) -> dict:
    """id -> mean (hold - placebo) gap on the surrogates, in rate points. Missing where never testable."""
    acc = {}
    for s in range(SURRG):
        sb = surrogate(bars, 1000 + s)
        atr = L.atr_series([b["h"] for b in sb], [b["l"] for b in sb], [b["c"] for b in sb], 14)
        for cid, series in build_levels(sb, tf, pivot_lookback).items():
            real, plc = _score(sb, series, atr, len(sb) // 2, halves=False)["all"]
            if real["rate"] is not None and plc["rate"] is not None:
                acc.setdefault(cid, []).append(real["rate"] - plc["rate"])
    return {k: sum(v) / len(v) for k, v in acc.items()}


def _stable(parts) -> Optional[bool]:
    vs = [L.verdict(parts[h][0], parts[h][1], min_n=HALF_MIN_N) for h in ("a", "b")]
    if "TOO_FEW_TESTS" in vs:
        return None
    return all(v == RESPECTED for v in vs)


def _fin(x) -> Optional[float]:
    return round(x, 4) if isinstance(x, float) and math.isfinite(x) else None


def _rows_tf(raw_bars, tf: str, pivot_lookback: int = 5):
    """(meta, rows) for one timeframe. Rows carry a p-value but no FDR decision yet: that needs the whole family."""
    bars = normalize(raw_bars)
    meta = {"bars": len(bars), "tested": 0, "note": None}
    if len(bars) < MIN_BARS:
        meta["note"] = "TOO_FEW_BARS"
        return meta, []
    atr = L.atr_series([b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], 14)
    levels = build_levels(bars, tf, pivot_lookback)
    meta["tested"] = len(levels)
    rows = []
    null = _null_gaps(bars, tf, pivot_lookback)
    for cid, series in levels.items():
        parts = _score(bars, series, atr, len(bars) // 2)
        d = null.get(cid, 0.0)
        parts = {h: (r, _shift(p, d)) for h, (r, p) in parts.items()}
        real, plc = parts["all"]
        if real["n"] == 0:
            continue
        hold, base = real["rate"], plc["rate"]
        pv = L.binom_sf(real["bounced"], real["n"], base / 100.0) if base is not None else None
        rows.append({"id": cid, "family": CATALOG[cid][0], "label": CATALOG[cid][1], "tests": real["n"],
                     "held": real["bounced"], "failed": real["failed"], "neutral": real["neutral"],
                     "hold_rate": hold, "placebo_rate": round(base, 1) if base is not None else None,
                     "p_value": round(pv, 4) if pv is not None else None, "fdr": None,
                     "verdict": L.verdict(real, plc, min_n=MIN_N), "stable": _stable(parts), "price": _fin(series[-1])})
    return meta, rows


def _decide(groups, q: float = FDR_Q) -> int:
    """One Benjamini-Hochberg pass over EVERY row in `groups` (the family of hypotheses this scan tested). A row keeps
    RESPECTED only if its p-value survives; otherwise the verdict collapses to NOT_DISTINGUISHABLE. Returns the family size."""
    rows = [r for g in groups for r in g]
    passes = L.benjamini_hochberg([r["p_value"] for r in rows], q)
    for r, ok in zip(rows, passes):
        r["fdr"] = ok
        if r["verdict"] == RESPECTED and ok is not True:
            r["verdict"] = "NOT_DISTINGUISHABLE"
    return sum(1 for r in rows if r["p_value"] is not None)


def _finish(meta: dict, rows: list, top: int, family: int) -> dict:
    def key(r):
        edge = (r["hold_rate"] - r["placebo_rate"]) if r["hold_rate"] is not None and r["placebo_rate"] is not None \
            else float("-inf")
        return (r["verdict"] == RESPECTED, edge, r["tests"])
    rows = sorted(rows, key=key, reverse=True)
    return {**meta, "family": family, "levels": rows[:max(0, int(top))]}


def scan_tf(raw_bars, tf: str, top: int = 3, pivot_lookback: int = 5) -> dict:
    """One timeframe on its own: the FDR family is that timeframe's candidates. `scan` uses the whole scan as the family."""
    meta, rows = _rows_tf(raw_bars, tf, pivot_lookback)
    fam = _decide([rows])
    return _finish(meta, rows, top, fam)


def _empty(note="TOO_FEW_BARS") -> dict:
    return {"bars": 0, "tested": 0, "note": note, "family": 0, "levels": []}


def scan(bars_by_tf: dict, *, top: int = 3, pivot_lookback: int = 5) -> dict:
    """Rank candidate levels per timeframe by edge over the random-line placebo, with one FDR correction over every
    candidate on every timeframe. See module docstring."""
    out = {"v": 1, "tfs": {}}
    src = bars_by_tf if isinstance(bars_by_tf, dict) else {}
    parts = {}
    for tf in TIMEFRAMES:
        try:
            parts[tf] = _rows_tf(src.get(tf), tf, pivot_lookback)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            parts[tf] = ({"bars": 0, "tested": 0, "note": "TOO_FEW_BARS"}, [])
    fam = _decide([rows for _, rows in parts.values()])
    for tf, (meta, rows) in parts.items():
        out["tfs"][tf] = _finish(meta, rows, top, fam)
    return out
