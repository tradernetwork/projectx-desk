"""Level-anchored stop and target for the ProjectX futures desk.

Pure and stdlib-only: no I/O, no clock, no globals. Deterministic, fails closed.

Rules (agreed with the trader, see docs/DECISIONS.md once integrated):

STOP   D0 = ceil(k_base x ATR / tick) ticks (floored at min_ticks, same as sizing.stop_from_atr). A respected level on
       the adverse side at distance d with D0 <= d <= k_cap x ATR pulls the stop to just beyond it
       (level -/+ max(2 ticks, 0.10 x ATR), rounded away from entry). If that lands wider than k_cap x ATR, no anchor:
       the base stop stands. Several qualifying levels: the nearest to entry. A level closer than D0 does nothing.
       The stop is never tighter than D0 and never wider than k_cap x ATR.
TARGET nearest respected level on the favorable side at >= 1R (stop distance). The level itself, rounded to the tick.
       None if there is none; no target is invented.

Only verdict ABOVE_RANDOM ("respected" in px_respect / px_levels) anchors anything. GEX marks positioning, not
targets: GEX-family levels never anchor even if one is passed in.

Contracts are NOT computed here. The caller re-floors contracts with the final stop: a wider stop means fewer
contracts, never more risk.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "AnchorResult",
    "RESPECTED_VERDICT",
    "anchor_stop_target",
    "heat",
    "projection",
]

RESPECTED_VERDICT = "ABOVE_RANDOM"   # px_respect.RESPECTED: the only verdict that may anchor anything
_BUFFER_TICKS = 2
_BUFFER_ATR = Decimal("0.10")


@dataclass(frozen=True)
class AnchorResult:
    ok: bool
    side: Optional[str] = None
    stop_price: Optional[float] = None
    stop_points: Optional[float] = None
    stop_ticks: Optional[int] = None
    risk_per_contract: Optional[float] = None   # stop_ticks x tick_value
    k_eff: Optional[float] = None               # final stop distance / atr
    base_points: Optional[float] = None         # D0, the un-anchored stop distance
    anchored_to: Optional[Dict[str, Any]] = None  # {"id", "tf"} or None
    target_price: Optional[float] = None
    target_points: Optional[float] = None
    target_level: Optional[Dict[str, Any]] = None  # {"id", "tf"} or None
    rr: Optional[float] = None
    breakeven_win_rate: Optional[float] = None  # percent
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fail(reason: str, side: Optional[str] = None) -> AnchorResult:
    return AnchorResult(ok=False, side=side, reasons=[reason])


def _num(value: object) -> Optional[float]:
    """Finite float or None. bool and non-numbers are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _pos(value: object) -> Optional[float]:
    v = _num(value)
    return v if v is not None and v > 0 else None


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def _fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s


def _label(lv: dict) -> str:
    tf = lv["tf"]
    return f"{tf} {lv['id']}" if tf else lv["id"]


def _clean_levels(levels: Sequence[object]) -> List[dict]:
    """Respected, well-formed, de-duplicated levels. Everything else is silently dropped."""
    out: List[dict] = []
    seen = set()
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        lid = lv.get("id")
        if not isinstance(lid, str) or not lid:
            continue
        if lv.get("verdict") != RESPECTED_VERDICT:
            continue
        fam = lv.get("family")
        if isinstance(fam, str) and fam.strip().lower() == "gex":
            continue
        price = _pos(lv.get("price"))
        if price is None:
            continue
        tf = lv.get("tf")
        tf = tf if isinstance(tf, str) and tf else None
        key = (lid, tf)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": lid, "tf": tf, "price": _dec(price), "stable": lv.get("stable")})
    return out


def anchor_stop_target(
    side: str,
    entry: float,
    atr: float,
    tick_size: float,
    tick_value: float,
    levels: Optional[Sequence[dict]] = None,
    k_base: float = 1.5,
    k_cap: float = 2.0,
    min_ticks: int = 4,
    require_stable: bool = False,
) -> AnchorResult:
    """Level-anchored stop and target. Never raises; bad input returns ok=False with a reason.

    `levels`: flat list of dicts (id, tf, price, verdict, optional family/stable). Only verdict ABOVE_RANDOM counts.
    `entry` must sit on the tick grid (a quote does); otherwise the stop price could not be an exact multiple of ticks.
    `min_ticks` matches sizing.stop_from_atr. If the tick-floored base stop already exceeds k_cap x ATR (tiny ATR
    relative to the tick) the result is ok=False: the cap is never exceeded.
    """
    try:
        return _anchor(side, entry, atr, tick_size, tick_value, levels, k_base, k_cap, min_ticks, require_stable)
    except Exception:  # noqa: BLE001  fail closed, never leak
        return _fail("ANCHOR error: unexpected input", side if side in ("LONG", "SHORT") else None)


def _anchor(side, entry, atr, tick_size, tick_value, levels, k_base, k_cap, min_ticks, require_stable) -> AnchorResult:
    if not isinstance(side, str) or side not in ("LONG", "SHORT"):
        return _fail("ANCHOR refused: side must be LONG or SHORT")
    names = (("entry", entry), ("atr", atr), ("tick_size", tick_size), ("tick_value", tick_value),
             ("k_base", k_base), ("k_cap", k_cap))
    vals = {}
    for name, raw in names:
        v = _pos(raw)
        if v is None:
            return _fail(f"ANCHOR refused: {name} must be a finite number > 0", side)
        vals[name] = v
    if k_cap < k_base:
        return _fail("ANCHOR refused: k_cap must be >= k_base", side)
    if isinstance(min_ticks, bool) or not isinstance(min_ticks, int) or min_ticks < 1:
        return _fail("ANCHOR refused: min_ticks must be an integer >= 1", side)
    if levels is None:
        levels = []
    if not isinstance(levels, (list, tuple)):
        return _fail("ANCHOR refused: levels must be a list", side)

    long_ = side == "LONG"
    e, a, ts = _dec(vals["entry"]), _dec(vals["atr"]), _dec(vals["tick_size"])
    if e % ts != 0:
        return _fail("ANCHOR refused: entry is not on the tick grid", side)
    cap = _dec(vals["k_cap"]) * a                      # k_cap x ATR, exact
    base_ticks = max(int((_dec(vals["k_base"]) * a / ts).to_integral_value(rounding=ROUND_CEILING)), min_ticks)
    d0 = base_ticks * ts
    if d0 > cap:
        return _fail("ANCHOR refused: base stop (tick-floored) exceeds k_cap x ATR", side)
    sgn = Decimal(-1) if long_ else Decimal(1)         # stop direction relative to entry
    if long_ and e - d0 <= 0:
        return _fail("ANCHOR refused: base stop would cross zero", side)

    reasons: List[str] = []
    cands = _clean_levels(levels)
    if require_stable:
        cands = [c for c in cands if c["stable"] is True]

    # ---- stop
    stop_dist = d0
    anchored: Optional[dict] = None
    buffer = max(_BUFFER_TICKS * ts, _BUFFER_ATR * a)
    adverse = []
    for c in cands:
        d = (e - c["price"]) if long_ else (c["price"] - e)
        if d > 0:
            adverse.append((d, c))
    qualifying = sorted(((d, c["id"], c["tf"] or "", c) for d, c in adverse if d0 <= d <= cap),
                        key=lambda t: (t[0], t[1], t[2]))
    if qualifying:
        d, _i, _t, c = qualifying[0]
        # beyond the level by the buffer, snapped to the tick grid away from entry
        raw_stop = c["price"] + sgn * buffer
        rounding = ROUND_FLOOR if long_ else ROUND_CEILING
        stop_px = (raw_stop / ts).to_integral_value(rounding=rounding) * ts
        dist = abs(e - stop_px)
        if dist <= cap and dist >= d0 and (stop_px < e if long_ else stop_px > e) and stop_px > 0:
            stop_dist, anchored = dist, c
            reasons.append(f"ANCHORED beyond {_label(c)} at {_fmt(c['price'])}")
        else:
            reasons.append(f"BASE {vals['k_base']}xATR: nearest respected level {_label(c)} "
                           f"needs a stop wider than {vals['k_cap']}xATR")
    else:
        reasons.append(f"BASE {vals['k_base']}xATR: no respected level between "
                       f"{vals['k_base']} and {vals['k_cap']} ATR")

    stop_px = e + sgn * stop_dist
    stop_ticks = int(stop_dist / ts)
    # hard invariants; belt and braces
    if not (d0 <= stop_dist <= cap) or stop_ticks < 1 or stop_px <= 0 or (stop_px >= e if long_ else stop_px <= e):
        return _fail("ANCHOR refused: stop failed invariant check", side)

    # ---- target
    target_px: Optional[Decimal] = None
    tlevel: Optional[dict] = None
    fav = []
    for c in cands:
        px = (c["price"] / ts).to_integral_value(rounding=ROUND_HALF_UP) * ts
        d = (px - e) if long_ else (e - px)
        if d >= stop_dist:
            fav.append((d, c["id"], c["tf"] or "", px, c))
    rr: Optional[float] = None
    be: Optional[float] = None
    tpoints: Optional[Decimal] = None
    if fav:
        fav.sort(key=lambda t: (t[0], t[1], t[2]))
        tpoints, _i, _t, target_px, tlevel = fav[0]
        rr_d = tpoints / stop_dist
        rr = float(rr_d)
        be = float(Decimal(100) / (1 + rr_d))
        reasons.append(f"TARGET {_label(tlevel)} at {_fmt(target_px)} ({rr:.2f}R)")
    else:
        reasons.append("TARGET none: no respected level >= 1R")

    return AnchorResult(
        ok=True,
        side=side,
        stop_price=float(stop_px),
        stop_points=float(stop_dist),
        stop_ticks=stop_ticks,
        risk_per_contract=float(Decimal(stop_ticks) * _dec(vals["tick_value"])),
        k_eff=float(stop_dist / a),
        base_points=float(d0),
        anchored_to={"id": anchored["id"], "tf": anchored["tf"]} if anchored else None,
        target_price=float(target_px) if target_px is not None else None,
        target_points=float(tpoints) if tpoints is not None else None,
        target_level={"id": tlevel["id"], "tf": tlevel["tf"]} if tlevel else None,
        rr=rr,
        breakeven_win_rate=be,
        reasons=reasons,
    )


# ------------------------------------------------------------------ helpers


def projection(balance_room: object, daily_room: object, risk_usd: object) -> Dict[str, Optional[float]]:
    """Dollars left in drawdown room and daily room if the stop fills: room - risk_usd, floored at 0.
    Any unreadable input (None, bool, non-number, non-finite) makes that figure None; a negative or
    unreadable risk makes both None. Never raises."""
    risk = _num(risk_usd)
    if risk is None or risk < 0:
        return {"drawdown_left": None, "daily_left": None}

    def left(room: object) -> Optional[float]:
        r = _num(room)
        return None if r is None else max(0.0, r - risk)

    return {"drawdown_left": left(balance_room), "daily_left": left(daily_room)}


def heat(open_stop_risk_usd: object, new_risk_usd: object, budget_base: object) -> Dict[str, Optional[float]]:
    """Total open stop risk after this trade, in dollars and as a percent of budget_base.
    total_usd is None if either risk is unreadable or negative; pct is None if total is None or
    budget_base is not a finite number > 0. Never raises."""
    o, n, b = _num(open_stop_risk_usd), _num(new_risk_usd), _num(budget_base)
    if o is None or n is None or o < 0 or n < 0:
        return {"total_usd": None, "pct": None}
    total = o + n
    pct = (100.0 * total / b) if (b is not None and b > 1e-9) else None
    if pct is not None and not math.isfinite(pct):
        pct = None
    return {"total_usd": total, "pct": pct}
