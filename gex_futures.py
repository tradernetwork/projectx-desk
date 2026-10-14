"""Convert underlying-space dealer-gamma levels into futures price space.

Pure functions, stdlib only, no I/O. The caller fetches the levels from any
dealer-gamma (GEX) source as a small dict (spot, flip, pin, walls, optional
flip_split flag), plus the futures last, the underlying spot and the tick
size, and passes them in.

Rules this module holds (docs/DECISIONS.md):
  - Gamma marks positioning, not targets. The flip is a regime boundary.
  - `flip_split` and every other uncertainty flag pass through untouched. This
    module never picks between the gex flip and the apex flip.
  - Fail closed. A stale or implausible conversion returns ok=False and NO
    levels, so nothing stale ever gets drawn.
"""
from __future__ import annotations

import copy
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

DISCLAIMER = "GEX marks positioning, not targets; the flip is a regime boundary, not a forecast."

# Source kinds. CASH_INDEX -> additive basis shift. ETF -> ratio scale.
CASH_INDEX = "cash_index"
ETF = "etf"

# --- Mapping table (data, not code) ---------------------------------------
# One row per futures contract root. `sources` is ordered by preference.
#   kind          CASH_INDEX or ETF, decides the conversion method
#   in_source     True if a typical GEX data source actually publishes levels for the symbol
#                 (cash indices like NDX/RUT/DJX usually do not; their ETFs do)
#   nominal_ratio rough futures/underlying ratio, used ONLY as a gross sanity
#                 check on the ETF ratio method (it drifts; wide tolerance)
# `approximate` marks mappings where the underlying is not the futures'
# underlying. `supported` False means refuse unless allow_unsupported=True.
MAPPING: dict[str, dict[str, Any]] = {
    "ES": {"sources": [
        {"symbol": "SPX", "kind": CASH_INDEX, "in_source": True},
        {"symbol": "SPY", "kind": ETF, "in_source": True, "nominal_ratio": 10.0}]},
    "NQ": {"sources": [
        {"symbol": "NDX", "kind": CASH_INDEX, "in_source": False},
        {"symbol": "QQQ", "kind": ETF, "in_source": True, "nominal_ratio": 41.0}]},
    "RTY": {"sources": [
        {"symbol": "RUT", "kind": CASH_INDEX, "in_source": False},
        {"symbol": "IWM", "kind": ETF, "in_source": True, "nominal_ratio": 10.0}]},
    "YM": {"sources": [
        {"symbol": "DJX", "kind": CASH_INDEX, "in_source": False},
        {"symbol": "DIA", "kind": ETF, "in_source": True, "nominal_ratio": 100.0}]},
    "GC": {"approximate": True,
           "note": "GLD is not gold spot: it carries fees and a drifting oz-per-share ratio.",
           "sources": [{"symbol": "GLD", "kind": ETF, "in_source": True, "nominal_ratio": 10.8}]},
    "CL": {"approximate": True, "supported": False,
           "note": "USO holds rolling futures, not spot crude; no better proxy is available.",
           "sources": [{"symbol": "USO", "kind": ETF, "in_source": True, "nominal_ratio": 1.0}]},
}
# Micros share their parent's underlying.
ALIASES: dict[str, str] = {
    "MES": "ES", "MNQ": "NQ", "M2K": "RTY", "MYM": "YM", "MGC": "GC", "MCL": "CL",
}

# Keys holding a single price level, and keys holding a list of dicts with a
# "strike" price. Everything else is copied through untouched.
PRICE_KEYS = ("spot", "flip", "flip_gex", "flip_apex", "pin",
              "wall_above", "wall_below", "call_wall", "put_wall")
STRIKE_LIST_KEYS = ("walls", "key_levels", "apex")


@dataclass
class FuturesLevels:
    ok: bool
    reason: str | None = None
    futures_symbol: str | None = None
    source_symbol: str | None = None
    method: str | None = None          # "additive" | "ratio"
    basis: float | None = None         # additive: fut - spot; ratio: 0.0 by construction
    ratio: float | None = None         # ratio method only
    approximate: bool = False
    tick_size: float | None = None
    notes: list[str] = field(default_factory=list)
    levels: dict[str, Any] | None = None   # None whenever ok is False


def _bad_price(v: Any) -> bool:
    return (isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) or v <= 0)


def _refuse(reason: str, **kw: Any) -> FuturesLevels:
    return FuturesLevels(ok=False, reason=reason, **kw)


def _decimals(tick: float) -> int:
    frac = f"{tick:.8f}".rstrip("0").partition(".")[2]
    return len(frac)


def _to_tick(x: float, tick: float) -> float:
    n = math.floor(x / tick + 0.5)
    return round(n * tick, _decimals(tick) + 2)


class _Bad(Exception):
    pass


def resolve_root(futures_symbol: str) -> str | None:
    s = (futures_symbol or "").strip().upper()
    s = ALIASES.get(s, s)
    return s if s in MAPPING else None


def convert_levels(
    levels: dict,
    futures_symbol: str,
    futures_last: float,
    underlying_spot: float,
    tick_size: float,
    asof_age_s: float | None,
    *,
    max_age_s: float = 300.0,
    max_basis_pct: float = 0.02,
    max_ratio_dev_pct: float = 0.35,
    source_symbol: str | None = None,
    allow_unsupported: bool = False,
) -> FuturesLevels:
    """Convert an underlying-space levels dict to futures space. Never raises."""
    if not isinstance(levels, dict) or not levels:
        return _refuse("levels must be a non-empty dict")
    if levels.get("error"):
        return _refuse("levels carry an error: no data to convert")
    root = resolve_root(futures_symbol)
    if root is None:
        return _refuse(f"unknown futures symbol {futures_symbol!r}")
    row = MAPPING[root]
    if row.get("supported", True) is False and not allow_unsupported:
        return _refuse(f"{root} is unsupported ({row.get('note', '')}); pass allow_unsupported=True to override",
                       futures_symbol=futures_symbol)

    src_sym = (source_symbol or levels.get("symbol") or "").strip().upper()
    src = next((s for s in row["sources"] if s["symbol"] == src_sym), None)
    if src is None:
        ok_syms = [s["symbol"] for s in row["sources"]]
        return _refuse(f"source {src_sym or '?'} is not a valid underlying for {root}; expected one of {ok_syms}",
                       futures_symbol=futures_symbol)

    if _bad_price(futures_last):
        return _refuse("futures_last must be a positive finite number", futures_symbol=futures_symbol)
    if _bad_price(underlying_spot):
        return _refuse("underlying_spot must be a positive finite number", futures_symbol=futures_symbol)
    if _bad_price(tick_size):
        return _refuse("tick_size must be a positive finite number", futures_symbol=futures_symbol)
    if (isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float))
            or not math.isfinite(max_age_s) or max_age_s < 0):
        return _refuse("max_age_s must be a non-negative number")
    if (asof_age_s is None or isinstance(asof_age_s, bool) or not isinstance(asof_age_s, (int, float))
            or not math.isfinite(asof_age_s) or asof_age_s < 0):
        return _refuse("asof_age_s missing or invalid; cannot prove freshness, refusing")
    if asof_age_s > max_age_s:
        return _refuse(f"levels are stale ({asof_age_s:.0f}s old, max {max_age_s:.0f}s); a stale level must not be drawn")

    approximate = bool(row.get("approximate"))
    notes: list[str] = []
    if row.get("note"):
        notes.append(row["note"])
    if src["kind"] == ETF and not row.get("approximate"):
        notes.append(f"{src_sym} is an ETF proxy; ratio scaling ignores tracking error and the ETF's own strike grid.")

    if src["kind"] == CASH_INDEX:
        method = "additive"
        basis = float(futures_last) - float(underlying_spot)
        if abs(basis) / underlying_spot > max_basis_pct:
            return _refuse(f"implausible basis {basis:+.2f} ({abs(basis) / underlying_spot:.2%} of spot, max {max_basis_pct:.2%})",
                           futures_symbol=futures_symbol, source_symbol=src_sym)
        ratio = None

        def fn(x: float) -> float:
            return x + basis
    else:
        method = "ratio"
        ratio = float(futures_last) / float(underlying_spot)
        nominal = src.get("nominal_ratio")
        if nominal and abs(ratio / nominal - 1.0) > max_ratio_dev_pct:
            return _refuse(f"implausible ratio {ratio:.4f} vs nominal {nominal} (dev {abs(ratio / nominal - 1):.0%}, max {max_ratio_dev_pct:.0%})",
                           futures_symbol=futures_symbol, source_symbol=src_sym)
        basis = 0.0

        def fn(x: float) -> float:
            return x * ratio

    def conv(v: Any) -> Any:
        if v is None:
            return None
        if _bad_price(v):
            raise _Bad(f"level value {v!r} is not a positive finite number")
        out = fn(float(v))
        if out <= 0:
            raise _Bad("converted level is non-positive")
        return _to_tick(out, tick_size)

    try:
        out_levels = copy.deepcopy(levels)
        for k in PRICE_KEYS:
            if k in out_levels:
                out_levels[k] = conv(out_levels[k])
        for k in STRIKE_LIST_KEYS:
            lst = out_levels.get(k)
            if isinstance(lst, list):
                for item in lst:
                    # Fail closed: an entry we cannot convert must never pass through still
                    # in underlying space, where it would be drawn on the wrong price.
                    if not (isinstance(item, dict) and "strike" in item):
                        raise _Bad(f"unrecognised entry in {k!r}; refusing rather than leave it unconverted")
                    item["strike"] = conv(item["strike"])
    except _Bad as e:
        return _refuse(str(e), futures_symbol=futures_symbol, source_symbol=src_sym)

    spot_in = levels.get("spot")
    if isinstance(spot_in, (int, float)) and not _bad_price(spot_in):
        drift = abs(spot_in - underlying_spot) / underlying_spot
        if drift > 0.005:
            notes.append(f"levels spot differs from live underlying_spot by {drift:.2%}; levels may lag the tape.")

    return FuturesLevels(
        ok=True, futures_symbol=futures_symbol.strip().upper(), source_symbol=src_sym,
        method=method, basis=round(basis, 4), ratio=None if ratio is None else round(ratio, 6),
        approximate=approximate, tick_size=float(tick_size), notes=notes, levels=out_levels,
    )


def _fmt(x: float, tick: float) -> str:
    return f"{x:.{max(2, _decimals(tick))}f}"


def describe_for_panel(result: FuturesLevels, last: float) -> "OrderedDict[str, str]":
    """Terse, ordered, text-ready fields for an AI-readable panel.

    Levels are listed high to low, each with its distance from `last` in points
    and ticks and whether it sits above or below. Never resolves a flip split.
    """
    out: "OrderedDict[str, str]" = OrderedDict()
    out["disclaimer"] = DISCLAIMER
    if not isinstance(result, FuturesLevels) or not result.ok or result.levels is None:
        out["status"] = "NO LEVELS: " + (getattr(result, "reason", None) or "invalid result")
        return out
    if _bad_price(last):
        out["status"] = "NO LEVELS: last must be a positive finite number"
        return out
    tick = result.tick_size or 0.01
    lv = result.levels
    out["status"] = "ok"
    out["futures"] = f"{result.futures_symbol} last {_fmt(last, tick)}"
    out["conversion"] = (f"{result.method} from {result.source_symbol}; basis {result.basis:+.2f}"
                         + (f"; ratio {result.ratio}" if result.ratio is not None else "")
                         + ("; APPROXIMATE" if result.approximate else ""))
    for n in result.notes:
        out[f"note_{len([k for k in out if k.startswith('note_')]) + 1}"] = n
    if lv.get("flip_split"):
        out["flip_split"] = ("TRUE: gex and apex flips are on opposite sides of spot; "
                             "regime is genuinely uncertain, both flips shown, none chosen")

    entries: list[tuple[str, float]] = []
    flip = lv.get("flip")
    if flip is not None:
        entries.append(("flip", flip))
    for k in ("flip_gex", "flip_apex"):
        v = lv.get(k)
        if v is not None and (lv.get("flip_split") or v != flip):
            entries.append((k, v))
    for k in ("pin", "wall_above", "wall_below", "call_wall", "put_wall"):
        if lv.get(k) is not None:
            entries.append((k, lv[k]))
    for k in STRIKE_LIST_KEYS:
        for i, item in enumerate(lv.get(k) or [], 1):
            if isinstance(item, dict) and item.get("strike") is not None:
                entries.append((f"{k}_{i}", item["strike"]))

    for name, price in sorted(entries, key=lambda e: -e[1]):
        diff = price - last
        ticks = int(round(diff / tick))
        side = "above" if diff > 0 else "below" if diff < 0 else "at"
        out[name] = f"{_fmt(price, tick)} | {diff:+.2f} pts | {ticks:+d} ticks | {side} last"
    return out
