"""Allowlisted views of Robinhood payloads. Pure, no I/O.

A view carries only validated enums, finite numbers and validated symbols. A missing, null, non-numeric or non-finite
number is OMITTED and its name listed in `unknown`: never defaulted to 0. No account number, nickname, balance or free
text from the provider ever passes through. Number strings ("10.0000") are the provider's shape (UNVERIFIED).
"""
from __future__ import annotations

import math
import re

SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_]{0,63}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

SIDES = {"buy", "sell"}
POS_SIDES = {"long", "short"}
ORDER_TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
ORDER_STATES = {"queued", "unconfirmed", "confirmed", "partially_filled", "filled", "cancelled", "canceled", "rejected",
                "failed", "pending", "expired"}
APPROVAL_STATES = {"pending": "pending", "approved": "approved", "declined": "declined", "rejected": "declined",
                   "expired": "expired", "cancelled": "expired", "canceled": "expired"}
RIGHTS = {"call", "put"}
ACCOUNT_STATES = {"active", "inactive", "restricted", "closed"}
ACCOUNT_TYPES = {"cash", "margin", "limited_margin", "ira", "other"}
OPTION_LEVELS = {"option_level_1", "option_level_2", "option_level_3", "option_level_4"}


def num(v):
    """A finite float or None. Booleans, NaN, infinities, blanks and junk are None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        try:
            x = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return x if math.isfinite(x) else None


def symbol(v):
    return v if isinstance(v, str) and SYMBOL.fullmatch(v) else None


def ident(v):
    return v if isinstance(v, str) and IDENT.fullmatch(v) else None


def enum(v, allowed):
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    return s if s in allowed else None


def _put_num(out, unknown, key, raw):
    x = num(raw)
    if x is None:
        unknown.append(key)
    else:
        out[key] = x


def _first(raw, *keys):
    for k in keys:
        if raw.get(k) is not None:
            return raw[k]
    return None


def _is_option(raw):
    return any(k in raw for k in ("strike_price", "option_type", "expiration_date", "chain_symbol"))


def position_view(raw):
    """One equity or option position, or None when there is no valid symbol."""
    if not isinstance(raw, dict):
        return None
    opt = _is_option(raw)
    sym = symbol(raw.get("chain_symbol") if opt and raw.get("chain_symbol") else raw.get("symbol"))
    if sym is None:
        return None
    out, unknown = {"symbol": sym, "asset": "option" if opt else "equity"}, []
    _put_num(out, unknown, "qty", raw.get("quantity"))
    _put_num(out, unknown, "avg_price", _first(raw, "average_buy_price", "average_price"))
    side = enum(raw.get("type"), POS_SIDES)
    if side:
        out["side"] = side
    elif not opt:
        out["side"] = "long"
    if opt:
        exp = raw.get("expiration_date")
        if isinstance(exp, str) and DATE.fullmatch(exp):
            out["expiry"] = exp
        else:
            unknown.append("expiry")
        _put_num(out, unknown, "strike", raw.get("strike_price"))
        right = enum(raw.get("option_type"), RIGHTS)
        if right:
            out["right"] = right
        else:
            unknown.append("right")
    out["unknown"] = unknown
    return out


def order_view(raw):
    if not isinstance(raw, dict):
        return None
    sym = symbol(raw.get("symbol") or raw.get("chain_symbol"))
    if sym is None:
        return None
    out, unknown = {"symbol": sym, "asset": "option" if _is_option(raw) else "equity"}, []
    oid = ident(raw.get("id"))
    if oid:
        out["id"] = oid
    ref = ident(raw.get("ref_id"))
    if ref:
        out["ref_id"] = ref                       # the desk's own idempotency id, echoed: how reconcile finds an order
    side = enum(raw.get("side"), SIDES)
    if side:
        out["side"] = side
    typ = enum(raw.get("type"), ORDER_TYPES)
    if typ:
        out["type"] = typ
    out["status"] = enum(raw.get("state"), ORDER_STATES) or "unknown"
    _put_num(out, unknown, "qty", raw.get("quantity"))
    if raw.get("price") is not None:
        _put_num(out, unknown, "price", raw.get("price"))
    if raw.get("average_price") is not None:
        _put_num(out, unknown, "avg_price", raw.get("average_price"))
    out["unknown"] = unknown
    return out


def po_ref(raw):
    po = raw.get("proposed_order")
    return po.get("ref_id") if isinstance(po, dict) else None


def approval_view(raw):
    if not isinstance(raw, dict):
        return None
    out, unknown = {}, []
    aid = ident(raw.get("approval_id") or raw.get("id"))
    if aid:
        out["id"] = aid
    ref = ident(raw.get("ref_id") or po_ref(raw))
    if ref:
        out["ref_id"] = ref
    ac = str(raw.get("asset_class") or "").lower()
    out["asset"] = "option" if "option" in ac else "equity"
    st = str(raw.get("state") or "").lower().replace("approval_state_", "")
    out["status"] = APPROVAL_STATES.get(st, "unknown")
    po = raw.get("proposed_order") if isinstance(raw.get("proposed_order"), dict) else {}
    sym = symbol(po.get("symbol"))
    if sym:
        out["symbol"] = sym
    side = enum(po.get("side"), SIDES)
    if side:
        out["side"] = side
    _put_num(out, unknown, "qty", po.get("quantity"))
    out["unknown"] = unknown
    return out


def account_view(raw):
    """The MCP account as the desk shows it: flags and enums only. The account number never leaves the reader."""
    if not isinstance(raw, dict):
        return None
    return {"agentic_allowed": raw.get("agentic_allowed") is True,
            "type": enum(raw.get("type"), ACCOUNT_TYPES) or "other",
            "state": enum(raw.get("state"), ACCOUNT_STATES) or "unknown",
            "option_level": enum(raw.get("option_level"), OPTION_LEVELS)}
