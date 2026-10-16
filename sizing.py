"""Position sizing math for the ProjectX futures desk.

Pure and stdlib-only: no I/O, no network, no mutated globals. This module is
the single source of truth for "how many contracts"; the UI displays its
output on every proposed trade and nothing else may do its own sizing.

Design stance: every function fails closed. Bad input raises SizingError, and
a trade that cannot be sized inside the risk budget is REFUSED (0 contracts)
rather than rounded up to fit. Contracts are always floored, so the risk
actually taken never exceeds the risk requested.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CONTRACT_SPECS",
    "ContractChoice",
    "ContractSpec",
    "SizeResult",
    "SizingError",
    "adx",
    "atr",
    "break_even_win_rate",
    "choose_contract",
    "describe_trade",
    "expectancy",
    "r_multiple",
    "risk_budget",
    "size_position",
    "spec_for",
    "stop_from_atr",
]


class SizingError(ValueError):
    """Raised on any input that cannot be sized safely. Callers must not
    catch this and fall back to a default size."""


# root -> (tick_size, tick_value, point_value, (relation, other_root))
# relation is "micro_of" (this root is the micro of other_root) or
# "full_size_of" (this root is the full-size contract for other_root's micro).
#
# These are static CME values for sanity checks and offline use. The live
# tickSize / tickValue from the ProjectX /Contract/search response MUST
# override them whenever present; pass them to spec_for().
CONTRACT_SPECS: Dict[str, Tuple[float, float, float, Tuple[str, str]]] = {
    "MES": (0.25, 1.25, 5.0, ("micro_of", "ES")),
    "ES": (0.25, 12.50, 50.0, ("full_size_of", "MES")),
    "MNQ": (0.25, 0.50, 2.0, ("micro_of", "NQ")),
    "NQ": (0.25, 5.00, 20.0, ("full_size_of", "MNQ")),
    "M2K": (0.10, 0.50, 5.0, ("micro_of", "RTY")),
    "RTY": (0.10, 5.00, 50.0, ("full_size_of", "M2K")),
    "MYM": (1.0, 0.50, 0.5, ("micro_of", "YM")),
    "YM": (1.0, 5.00, 5.0, ("full_size_of", "MYM")),
    "MCL": (0.01, 1.00, 100.0, ("micro_of", "CL")),
    "CL": (0.01, 10.00, 1000.0, ("full_size_of", "MCL")),
    "MGC": (0.1, 1.00, 10.0, ("micro_of", "GC")),
    "GC": (0.1, 10.00, 100.0, ("full_size_of", "MGC")),
}


@dataclass(frozen=True)
class ContractSpec:
    root: str
    tick_size: float
    tick_value: float
    point_value: float
    micro_of: Optional[str]
    full_size_of: Optional[str]
    live: bool  # True when tick_size/tick_value came from the live response


MAX_MAGNITUDE = 1e12      # no price, tick count or dollar figure in this domain is anywhere near this; beyond it Decimal/float math is unsafe


def _num(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SizingError(f"{name} must be a number, got {type(value).__name__}")
    try:
        v = float(value)
    except OverflowError:
        raise SizingError(f"{name} is too large") from None
    if not math.isfinite(v):
        raise SizingError(f"{name} must be finite")
    if abs(v) > MAX_MAGNITUDE:
        raise SizingError(f"{name} is out of range")
    return v


def _finite_positive(name: str, value: object) -> float:
    v = _num(name, value)
    if v <= 0:
        raise SizingError(f"{name} must be > 0")
    return v


def _finite(name: str, value: object) -> float:
    return _num(name, value)


def _dec(x: float) -> Decimal:
    # repr round-trips, so this is the exact decimal the caller typed
    return Decimal(repr(float(x)))


def _root_from_id(contract_id_or_root: str) -> str:
    if not isinstance(contract_id_or_root, str) or not contract_id_or_root.strip():
        raise SizingError("contract id or root must be a non-empty string")
    text = contract_id_or_root.strip().upper()
    parts = text.split(".")
    if len(parts) >= 4 and parts[0] == "CON":
        return parts[3]  # CON.F.US.MES.Z25 -> MES
    if len(parts) == 1:
        return parts[0]
    raise SizingError(f"cannot parse contract id: {contract_id_or_root!r}")


def spec_for(
    contract_id_or_root: str,
    live_tick_size: Optional[float] = None,
    live_tick_value: Optional[float] = None,
) -> ContractSpec:
    """Resolve a contract spec. Live tick values override the static table.

    Fails closed: unknown root, or any non-positive / non-finite value,
    raises SizingError.
    """
    root = _root_from_id(contract_id_or_root)
    row = CONTRACT_SPECS.get(root)
    if row is None:
        raise SizingError(f"unknown contract root: {root!r}")
    tick_size, tick_value, _static_point, (relation, other) = row

    live = False
    if live_tick_size is not None:
        tick_size = _finite_positive("live_tick_size", live_tick_size)
        live = True
    if live_tick_value is not None:
        tick_value = _finite_positive("live_tick_value", live_tick_value)
        live = True
    point_value = tick_value / tick_size
    return ContractSpec(
        root=root,
        tick_size=tick_size,
        tick_value=tick_value,
        point_value=point_value,
        micro_of=other if relation == "micro_of" else None,
        full_size_of=other if relation == "full_size_of" else None,
        live=live,
    )


# ---------------------------------------------------------------- indicators


def _check_series(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int):
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise SizingError("n must be a positive integer")
    if not (len(highs) == len(lows) == len(closes)):
        raise SizingError("highs, lows and closes must be the same length")
    for series in (highs, lows, closes):
        for v in series:
            if not math.isfinite(float(v)):
                raise SizingError("price series contains NaN or inf")


def _true_ranges(highs, lows, closes):
    return [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(highs))
    ]


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Optional[float]:
    """Wilder ATR. Seeded with the simple mean of the first n true ranges,
    then smoothed (prev*(n-1) + TR) / n. Needs n+1 bars, else None."""
    _check_series(highs, lows, closes, n)
    if len(closes) < n + 1:
        return None
    trs = _true_ranges(highs, lows, closes)
    value = sum(trs[:n]) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14
) -> Optional[Tuple[float, float, float]]:
    """Wilder ADX. Returns (adx, plus_di, minus_di), or None with fewer than
    2n+1 bars. Smoothed TR/+DM/-DM seed as the sum of the first n values and
    roll as S - S/n + x; ADX seeds as the mean of the first n DX values."""
    _check_series(highs, lows, closes, n)
    if len(closes) < 2 * n + 1:
        return None
    trs = _true_ranges(highs, lows, closes)
    pdm, mdm = [], []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm.append(up if (up > down and up > 0) else 0.0)
        mdm.append(down if (down > up and down > 0) else 0.0)

    s_tr, s_p, s_m = sum(trs[:n]), sum(pdm[:n]), sum(mdm[:n])

    def di_dx(tr_s: float, p_s: float, m_s: float) -> Tuple[float, float, float]:
        if tr_s <= 0:
            return 0.0, 0.0, 0.0
        pdi, mdi = 100.0 * p_s / tr_s, 100.0 * m_s / tr_s
        total = pdi + mdi
        dx = 0.0 if total == 0 else 100.0 * abs(pdi - mdi) / total
        return pdi, mdi, dx

    pdi, mdi, dx = di_dx(s_tr, s_p, s_m)
    dxs = [dx]
    for i in range(n, len(trs)):
        s_tr = s_tr - s_tr / n + trs[i]
        s_p = s_p - s_p / n + pdm[i]
        s_m = s_m - s_m / n + mdm[i]
        pdi, mdi, dx = di_dx(s_tr, s_p, s_m)
        dxs.append(dx)

    value = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        value = (value * (n - 1) + dx) / n
    return value, pdi, mdi


# -------------------------------------------------------------------- sizing


@dataclass(frozen=True)
class SizeResult:
    contracts: int
    risk_per_contract: float
    actual_risk: float
    unused_risk: float
    binding: str  # "granularity" | "max_contracts" | "risk"
    refused: bool
    reason: str


def size_position(
    risk_dollars: float,
    stop_ticks: float,
    tick_value: float,
    max_contracts: Optional[int] = None,
) -> SizeResult:
    """Whole contracts whose stop-out loss fits inside risk_dollars.

    Always floors. Never returns actual_risk > risk_dollars. If not even one
    contract fits the result is refused with contracts == 0.
    """
    risk = _finite_positive("risk_dollars", risk_dollars)
    ticks = _finite_positive("stop_ticks", stop_ticks)
    if ticks != int(ticks):
        raise SizingError("stop_ticks must be a whole number of ticks")       # a stop cannot sit between ticks
    tv = _finite_positive("tick_value", tick_value)
    if max_contracts is not None:
        if isinstance(max_contracts, bool) or not isinstance(max_contracts, int) or max_contracts < 1:
            raise SizingError("max_contracts must be an integer >= 1")

    rpc_dec = _dec(ticks) * _dec(tv)
    by_risk = int((_dec(risk) // rpc_dec))  # exact decimal floor
    rpc = float(rpc_dec)

    contracts = by_risk
    binding = "risk"
    if max_contracts is not None and by_risk > max_contracts:
        contracts = max_contracts
        binding = "max_contracts"
    if contracts == 0:
        binding = "granularity"

    actual_dec = rpc_dec * contracts
    actual = float(actual_dec)
    unused = float(_dec(risk) - actual_dec)
    if contracts == 0:
        reason = (
            f"one contract risks ${rpc:.2f}, more than the ${risk:.2f} budget; "
            "tighten the stop, trade a micro, or skip"
        )
    else:
        reason = ""
    return SizeResult(
        contracts=contracts,
        risk_per_contract=rpc,
        actual_risk=actual,
        unused_risk=unused,
        binding=binding,
        refused=contracts == 0,
        reason=reason,
    )


def risk_budget(
    equity_or_buffer: float,
    risk_pct: float,
    daily_loss_remaining: Optional[float] = None,
    drawdown_buffer_remaining: Optional[float] = None,
) -> float:
    """Dollars allowed at risk on one trade.

    min(risk_pct * equity_or_buffer, daily_loss_remaining,
    drawdown_buffer_remaining), counting only limits that were provided.
    risk_pct is a fraction (0.01 is 1%). Fails closed to 0.0 when the base or
    any provided limit is <= 0.
    """
    base = _finite("equity_or_buffer", equity_or_buffer)
    pct = _finite("risk_pct", risk_pct)
    if not (0 < pct <= 1):
        raise SizingError("risk_pct must be a fraction in (0, 1]")
    limits = []
    for name, val in (
        ("daily_loss_remaining", daily_loss_remaining),
        ("drawdown_buffer_remaining", drawdown_buffer_remaining),
    ):
        if val is not None:
            limits.append(_finite(name, val))
    if base <= 0 or any(x <= 0 for x in limits):
        return 0.0
    return float(min([base * pct] + limits))


def stop_from_atr(atr_value: float, tick_size: float, k: float = 1.5, min_ticks: int = 4) -> int:
    """Stop distance in whole ticks: ceil(k * ATR / tick_size), floored at
    min_ticks. Rounds UP so the stop is never tighter than the ATR rule."""
    a = _finite_positive("atr_value", atr_value)
    ts = _finite_positive("tick_size", tick_size)
    kk = _finite_positive("k", k)
    if isinstance(min_ticks, bool) or not isinstance(min_ticks, int) or min_ticks < 1:
        raise SizingError("min_ticks must be an integer >= 1")
    raw = _dec(kk) * _dec(a) / _dec(ts)
    ticks = int(raw.to_integral_value(rounding="ROUND_CEILING"))
    return max(ticks, min_ticks)


# ------------------------------------------------------------ micro vs full


@dataclass(frozen=True)
class ContractChoice:
    market: str
    risk_dollars: float
    stop_points: float
    options: Mapping[str, SizeResult]  # root -> sizing
    recommended: Optional[str]
    refused: bool
    reason: str
    adx_value: Optional[float]  # display only, never used in the decision


def choose_contract(
    root: str,
    risk_dollars: float,
    stop_points: float,
    specs: Mapping[str, tuple] = CONTRACT_SPECS,
    *,
    adx_value: Optional[float] = None,
    full_min_contracts: int = 1,
) -> ContractChoice:
    """Compare micro vs full-size for the same market at the same stop.

    Full-size is recommended only if it fits inside risk_dollars with at
    least full_min_contracts contracts AND wastes strictly less risk than the
    micro would. Otherwise micro. Refused if neither fits. adx_value is
    echoed for display; no ADX policy lives here.
    """
    risk = _finite_positive("risk_dollars", risk_dollars)
    stop_pts = _finite_positive("stop_points", stop_points)
    if isinstance(full_min_contracts, bool) or not isinstance(full_min_contracts, int) or full_min_contracts < 1:
        raise SizingError("full_min_contracts must be an integer >= 1")
    if adx_value is not None:
        adx_value = _finite("adx_value", adx_value)

    r = _root_from_id(root)
    if r not in specs:
        raise SizingError(f"unknown contract root: {r!r}")
    relation, other = specs[r][3]
    if relation == "micro_of":
        micro_root, full_root = r, other
    else:
        micro_root, full_root = other, r
    for rt in (micro_root, full_root):
        if rt not in specs:
            raise SizingError(f"missing spec for paired contract: {rt!r}")

    def size_for(rt: str) -> SizeResult:
        tick_size, tick_value = specs[rt][0], specs[rt][1]
        ticks = int((_dec(stop_pts) / _dec(tick_size)).to_integral_value(rounding="ROUND_CEILING"))
        return size_position(risk, max(ticks, 1), tick_value)

    micro, full = size_for(micro_root), size_for(full_root)
    options = {micro_root: micro, full_root: full}

    full_ok = (not full.refused) and full.contracts >= full_min_contracts
    if full_ok and full.unused_risk < micro.unused_risk:
        rec, reason = full_root, "full-size fits and wastes less risk than the micro"
    elif not micro.refused:
        rec, reason = micro_root, "micro hits the target risk more closely or full-size does not fit"
    elif full_ok:
        rec, reason = full_root, "only full-size fits"
    else:
        rec, reason = None, "neither micro nor full-size fits inside the risk budget"
    return ContractChoice(
        market=full_root,
        risk_dollars=risk,
        stop_points=stop_pts,
        options=options,
        recommended=rec,
        refused=rec is None,
        reason=reason,
        adx_value=adx_value,
    )


# -------------------------------------------------------------- R and edge


def _side(side: str) -> str:
    s = str(side).strip().lower()
    if s in ("long", "buy", "bid"):
        return "long"
    if s in ("short", "sell", "ask"):
        return "short"
    raise SizingError(f"unknown side: {side!r}")


def r_multiple(entry: float, exit: float, stop: float, side: str) -> float:
    """Result of a trade in units of initial risk. The stop must sit on the
    losing side of entry."""
    e, x, s = _finite("entry", entry), _finite("exit", exit), _finite("stop", stop)
    sd = _side(side)
    risk = (e - s) if sd == "long" else (s - e)
    if risk <= 0:
        raise SizingError("stop must be on the losing side of entry")
    return ((x - e) if sd == "long" else (e - x)) / risk


def expectancy(r_list: Sequence[float]) -> Optional[float]:
    """Mean R per trade, or None with no trades (no sample is not zero edge)."""
    vals = [_finite("r", v) for v in r_list]
    if not vals:
        return None
    return sum(vals) / len(vals)


def break_even_win_rate(avg_win_r: float, avg_loss_r: float) -> float:
    """Win rate at which expectancy is zero: L / (W + L). Both are magnitudes."""
    w = _finite_positive("avg_win_r", abs(_finite("avg_win_r", avg_win_r)))
    l = _finite_positive("avg_loss_r", abs(_finite("avg_loss_r", avg_loss_r)))
    return l / (w + l)


# ------------------------------------------------------------------ the UI


def describe_trade(
    contract_id_or_root: str,
    side: str,
    entry: float,
    stop_price: float,
    risk_dollars: float,
    *,
    risk_budget_dollars: Optional[float] = None,
    max_contracts: Optional[int] = None,
    live_tick_size: Optional[float] = None,
    live_tick_value: Optional[float] = None,
) -> "OrderedDict[str, object]":
    """Every number the UI shows for a proposed trade, in display order."""
    spec = spec_for(contract_id_or_root, live_tick_size, live_tick_value)
    sd = _side(side)
    e = _finite_positive("entry", entry)
    sp = _finite_positive("stop_price", stop_price)
    if (sd == "long" and sp >= e) or (sd == "short" and sp <= e):
        raise SizingError("stop must be on the losing side of entry")
    budget = _finite_positive(
        "risk_budget_dollars", risk_dollars if risk_budget_dollars is None else risk_budget_dollars
    )

    ticks = int((abs(_dec(e) - _dec(sp)) / _dec(spec.tick_size)).to_integral_value(rounding="ROUND_CEILING"))
    ticks = max(ticks, 1)
    # Never size beyond the room that actually remains: a caller that passes a larger risk than the budget (an MCP size
    # request) gets the budget-limited answer, not one that shows a size the account cannot afford.
    sized = size_position(min(risk_dollars, budget), ticks, spec.tick_value, max_contracts)

    out: "OrderedDict[str, object]" = OrderedDict()
    out["contract"] = spec.root
    out["side"] = "BUY (long)" if sd == "long" else "SELL (short)"
    out["entry"] = e
    out["stop_price"] = sp
    out["stop_ticks"] = ticks
    out["tick_value"] = spec.tick_value
    out["risk_per_contract"] = sized.risk_per_contract
    out["contracts"] = sized.contracts
    out["total_risk_dollars"] = sized.actual_risk
    out["pct_of_risk_budget_used"] = 100.0 * sized.actual_risk / budget
    for k in (1, 2, 3):
        out[f"reward_{k}r_dollars"] = sized.actual_risk * k
    out["binding_constraint"] = sized.binding
    out["refused"] = sized.refused
    out["reason"] = sized.reason
    return out


# ---- volatility / trend risk scaler (HYPOTHESIS thresholds, docs/SIZING_POLICY.md section 4) ----------
# Reduce-only by construction: every multiplier is <= 1, so ADX and ATR can shrink the risk the trader
# chose but never grow it. Unknown inputs shrink it too (fail toward smaller). Every step carries a
# plain-English reason so the desk can always say WHY it is on this many contracts.
ADX_CHOP = 20.0          # below this the market is not trending: stops get hit more, size down one notch
ADX_CHOP_MULT = 0.67     # one notch on the 1/2/3/5 risk chips (3 -> 2)
ATR_HIGH_PCTILE = 80.0   # volatility in the top fifth of its own recent history
ATR_HIGH_MULT = 0.67
UNKNOWN_MULT = 0.75      # unknown ADX/ATR context: be smaller, say so
MIN_SCALE = 0.25


def _ordinal(n: float) -> str:
    n = int(round(_finite("percentile", n)))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def risk_scale(adx_value: Optional[float], atr_pctile: Optional[float]) -> "OrderedDict[str, object]":
    """Multiplier in [0.25, 1.0] applied to the chosen risk, with the reasons. Never above 1."""
    steps, mult = [], 1.0
    if adx_value is None or adx_value != adx_value:
        mult *= UNKNOWN_MULT
        steps.append(("ADX unknown", UNKNOWN_MULT))
    elif adx_value < ADX_CHOP:
        mult *= ADX_CHOP_MULT
        steps.append((f"ADX {adx_value:.1f} < {ADX_CHOP:.0f}: chop, stops get hit more", ADX_CHOP_MULT))
    else:
        steps.append((f"ADX {adx_value:.1f} >= {ADX_CHOP:.0f}: trending enough, no cut", 1.0))
    if atr_pctile is None or atr_pctile != atr_pctile:
        mult *= UNKNOWN_MULT
        steps.append(("ATR percentile unknown", UNKNOWN_MULT))
    elif atr_pctile > ATR_HIGH_PCTILE:
        mult *= ATR_HIGH_MULT
        steps.append((f"ATR in the {_ordinal(atr_pctile)} percentile: volatile, wide stops", ATR_HIGH_MULT))
    else:
        steps.append((f"ATR in the {_ordinal(atr_pctile)} percentile: normal, no cut", 1.0))
    mult = max(MIN_SCALE, min(1.0, mult))
    out: "OrderedDict[str, object]" = OrderedDict()
    out["multiplier"] = round(mult, 4)
    out["reasons"] = [f"{t} (x{m:g})" for t, m in steps]
    out["reduced"] = mult < 1.0
    out["status"] = "HYPOTHESIS thresholds, untested"
    return out
