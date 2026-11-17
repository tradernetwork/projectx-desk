"""Robinhood order limits. Every limit fails closed: a zero, blank, negative, non-finite or garbled money or quantity cap is
None, and None refuses every order (`cap_unset`). `PX_RH_TRADING` is on only for a clear truthy word."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

DEFAULT_TTL_S = 120
DEFAULT_QUOTE_AGE_S = 5


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _pos_float(v) -> Optional[float]:
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _pos_int(v) -> Optional[int]:
    x = _pos_float(v)
    return int(x) if x is not None and x >= 1 else None


def _seconds(v, default: int) -> int:
    x = _pos_float(v)
    return int(x) if x is not None and x >= 1 else default


@dataclass(frozen=True)
class RhLimits:
    trading: bool = False
    max_notional: Optional[float] = None
    max_qty: Optional[int] = None
    ticket_ttl_s: int = DEFAULT_TTL_S
    quote_max_age_s: int = DEFAULT_QUOTE_AGE_S

    @classmethod
    def from_env(cls, environ) -> "RhLimits":
        e = environ
        return cls(trading=_truthy(e.get("PX_RH_TRADING")),
                   max_notional=_pos_float(e.get("PX_RH_MAX_NOTIONAL")),
                   max_qty=_pos_int(e.get("PX_RH_MAX_QTY")),
                   ticket_ttl_s=_seconds(e.get("PX_RH_TICKET_TTL_S"), DEFAULT_TTL_S),
                   quote_max_age_s=_seconds(e.get("PX_RH_QUOTE_MAX_AGE_S"), DEFAULT_QUOTE_AGE_S))
