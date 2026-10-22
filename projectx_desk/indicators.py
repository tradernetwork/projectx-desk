"""Which indicators deserve a place on the chart right now, per timeframe? Pure functions, stdlib only, NO I/O.

Two different questions, kept apart on purpose:
  * LEVELS (ema, sma, boll, keltner, fib, prior): shown only when the existing respect scan (all three gates, see
    respect.py) calls them respected on THIS timeframe AND price is close enough to matter (proximity gate, in ATR).
    This module never re-scores anything and never touches the gates.
  * OSCILLATORS (macd, rsi): regime gates, "is it live right now". They say nothing about edge, so the reason is
    ACTIVE, never RESPECTED. MACD sitting flat near zero after a big swing is exactly what the gate hides.
Closed bars only; every value at bar i uses bars <= i, so there is no lookahead.
"""
import math
import re
from typing import Optional, Sequence

from projectx_desk import levels as L
from projectx_desk.respect import CATALOG, TIMEFRAMES

ALWAYS_ON = ("ema21", "vwap", "vol")           # drawn regardless; never listed as a gated level
RESPECTED_VERDICTS = frozenset({"RESPECTED", "ABOVE_RANDOM"})
SERIES_FAMILIES = frozenset({"ema", "sma", "boll"})     # the UI draws these as indicator lines
MAX_LEVELS = 3
NEAR_ATR = {"fib": 1.5}                         # |price - close| limit in ATR per family; everything else uses NEAR_DEFAULT
NEAR_DEFAULT = 3.0
MACD_MIN_BARS = 100
MACD_WINDOW = 200
MACD_PCTILE = 0.60
RSI_N = 14
RSI_LOOK = 5
RSI_LO, RSI_HI = 30.0, 70.0
DEFAULT_ROOTS = ("MES", "MNQ", "MGC")
MAX_ROOTS = 4
_ROOT_RE = re.compile(r"^[A-Z0-9]{1,6}$")


def _fin(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if isinstance(x, bool) or not math.isfinite(f):
        return None
    return f


# ---------------------------------------------------------------------------------------------- indicator math

def macd_hist(closes: Sequence[float], fast: int = 12, slow: int = 26, sig: int = 9) -> list:
    """MACD histogram (macd line minus its signal EMA), None where undefined. Value at i uses closes[:i+1] only."""
    out = [None] * len(closes)
    ef, es = L.ema_series(list(closes), fast), L.ema_series(list(closes), slow)
    line = [(a - b) if a is not None and b is not None else None for a, b in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    if first is None:
        return out
    sg = L.ema_series(line[first:], sig)
    for j, s in enumerate(sg):
        if s is not None:
            out[first + j] = line[first + j] - s
    return out


def rsi_series(closes: Sequence[float], n: int = RSI_N) -> list:
    """Wilder RSI, None until n+1 closes exist."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def val(g, l):
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / l)
    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = val(ag, al)
    return out


def percentile_rank(x: float, window: Sequence[float]) -> Optional[float]:
    """Fraction of `window` values that are <= x, in 0..1. None for an empty window."""
    vals = [v for v in window if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v <= x) / len(vals)


def macd_gate(closes: Sequence[float]) -> tuple:
    """(active, pctile). Active when |hist| on the last closed bar is >= the 60th percentile of |hist| over the
    trailing 200 closed bars. Needs >= 100 bars; otherwise (False, None): not shown."""
    if len(closes) < MACD_MIN_BARS:
        return False, None
    h = macd_hist(closes)
    last = h[-1]
    if last is None:
        return False, None
    window = [abs(v) for v in h[-MACD_WINDOW:] if v is not None]
    p = percentile_rank(abs(last), window)
    if p is None:
        return False, None
    return p >= MACD_PCTILE, round(p, 4)


def rsi_gate(closes: Sequence[float]) -> tuple:
    """(active, last_rsi). Active when any of the last 5 closed bars had RSI(14) <= 30 or >= 70."""
    r = rsi_series(closes)
    recent = [v for v in r[-RSI_LOOK:] if v is not None]
    last = r[-1] if r else None
    if not recent:
        return False, None
    return any(v <= RSI_LO or v >= RSI_HI for v in recent), (round(last, 2) if last is not None else None)


def atr_last(bars) -> Optional[float]:
    """ATR(14) on the last closed bar, from normalized bars (respect.normalize shape). None when unknown."""
    if not bars:
        return None
    a = L.atr_series([b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], 14)[-1]
    f = _fin(a)
    return f if f is not None and f > 0 else None


# ---------------------------------------------------------------------------------------------- selection

def _period(cid: str, family: str) -> Optional[int]:
    if family not in ("ema", "sma"):
        return None
    m = re.search(r"(\d+)$", cid)
    return int(m.group(1)) if m else None


def select_levels(rows, price: Optional[float], atr: Optional[float], max_levels: int = MAX_LEVELS):
    """(shown, valid_count). `rows` are sanitized respect rows for ONE timeframe, best first (order is kept).
    A row is shown only if: catalog id, respected verdict, not always-on, finite level price, and within the proximity
    gate (fib 1.5 ATR, everything else 3 ATR). Unknown price or ATR hides every level (fail closed)."""
    valid, shown = 0, []
    for r in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(r, dict) or r.get("id") not in CATALOG:
            continue
        valid += 1
        cid = r["id"]
        family, label = CATALOG[cid]
        if cid in ALWAYS_ON or r.get("verdict") not in RESPECTED_VERDICTS:
            continue
        lp, px, a = _fin(r.get("price")), _fin(price), _fin(atr)
        if lp is None or px is None or a is None or a <= 0:
            continue
        dist = abs(lp - px) / a
        if dist > NEAR_ATR.get(family, NEAR_DEFAULT):
            continue
        if len(shown) >= max_levels:
            continue
        stable = r.get("stable")
        shown.append({"id": cid, "label": label, "family": family,
                      "draw": "series" if family in SERIES_FAMILIES else "horizontal",
                      "period": _period(cid, family), "price": lp, "reason": "RESPECTED",
                      "stable": stable if isinstance(stable, bool) else None,
                      "held": _int(r.get("held")), "tests": _int(r.get("tests")), "dist_atr": round(dist, 2)})
    return shown, valid


def _int(v) -> int:
    f = _fin(v)
    return int(f) if f is not None and f >= 0 else 0


def evaluate(tf_res, bars) -> dict:
    """Everything the route and the grade log need for one timeframe, from the (sanitized) respect result for that
    timeframe and the already-fetched closed bars (normalized). No I/O, no rescoring."""
    closes = [b["c"] for b in bars]
    price = closes[-1] if closes else None
    atr = atr_last(bars)
    rows = (tf_res or {}).get("levels") if isinstance(tf_res, dict) else []
    shown, valid = select_levels(rows, price, atr)
    osc, macd_p, rsi_v = [], None, None
    active, macd_p = macd_gate(closes)
    if active:
        osc.append({"id": "macd", "label": "MACD", "reason": "ACTIVE", "pctile": macd_p})
    active, rsi_v = rsi_gate(closes)
    if active:
        osc.append({"id": "rsi", "label": "RSI", "reason": "ACTIVE", "pctile": None})
    last_t = bars[-1]["t"] if bars else None
    return {"asof": int(last_t) if last_t is not None else None, "price": price, "atr": atr,
            "levels": shown, "oscillators": osc, "hidden": max(0, valid - len(shown)),
            "candidates": [r for r in rows if isinstance(r, dict) and r.get("id") in CATALOG] if isinstance(rows, list) else [],
            "macd_pctile": macd_p, "rsi": rsi_v}


def payload(cid, tf, ev) -> dict:
    return {"status": "ok", "code": "", "contract": cid, "tf": tf, "asof": ev["asof"], "always": ["ema21", "vwap", "vol"],
            "levels": ev["levels"], "oscillators": ev["oscillators"], "hidden": ev["hidden"]}


def status_payload(status, code, cid=None, tf=None) -> dict:
    return {"status": status, "code": code, "contract": cid, "tf": tf, "asof": None, "always": ["ema21", "vwap", "vol"],
            "levels": [], "oscillators": [], "hidden": 0}


# ---------------------------------------------------------------------------------------------- tracking config

def parse_roots(raw) -> list:
    """PX_TRACK_ROOTS: comma list of futures roots. Blank or garbled -> default; '0' -> none; capped at MAX_ROOTS."""
    if raw is None or not str(raw).strip():
        return list(DEFAULT_ROOTS)
    s = str(raw).strip()
    if s == "0":
        return []
    out = []
    for part in s.split(","):
        p = part.strip().upper()
        if not _ROOT_RE.match(p):
            return list(DEFAULT_ROOTS)             # garbled anywhere -> the whole value is distrusted
        if p not in out:
            out.append(p)
    return out[:MAX_ROOTS] or list(DEFAULT_ROOTS)


_MONTHS = "FGHJKMNQUVXZ"


def front_contract(contracts, root: str, now_year: int, now_month: int) -> Optional[str]:
    """Id of the nearest-expiry contract for `root` from a search result (ids look like CON.F.US.MNQ.Z25).
    Expired months are skipped. None when nothing matches. Pure."""
    best = None
    for c in contracts if isinstance(contracts, list) else []:
        cid = c.get("id") if isinstance(c, dict) else None
        if not isinstance(cid, str):
            continue
        parts = cid.split(".")
        if len(parts) != 5 or parts[3] != root or len(parts[4]) < 2 or parts[4][0] not in _MONTHS:
            continue
        digits = parts[4][1:]
        if not digits.isdigit():
            continue
        y = int(digits)
        y = 2000 + y if y >= 10 else (now_year // 10) * 10 + y      # one digit: this decade, next decade if already past
        if len(digits) == 1 and y < now_year - 1:
            y += 10
        m = _MONTHS.index(parts[4][0]) + 1
        if (y, m) < (now_year, now_month):
            continue
        key = (y, m)
        if best is None or key < best[0]:
            best = (key, cid)
    return best[1] if best else None
