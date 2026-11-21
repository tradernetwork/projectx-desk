"""Allowlisted reduction of the Public sidecar's /ai/state JSON (Round 36). Pure: no I/O, no float, never raises.

The sidecar already allowlists its own surface; this module does it again on our side, because a link is only as safe as
the end that reads it. Every field below is one of:
  * a number, kept as a canonical decimal STRING (the JSON is parsed with parse_float=Decimal, so a float never exists);
  * an enum from a fixed set copied from the sidecar's vocabulary, or a short word matching a strict pattern;
  * a validated symbol (equity ticker or OSI option symbol), or a YYYY-MM-DD day.
Nothing else crosses: no account number, no ticket id, no order reference, no confirm hash, no proposal id, no ledger event
id, no free text. A field that fails its check is dropped; a section the sidecar could not read stays None (UNKNOWN), never
an empty list. Stream mode drops every dollar key, recursively.
"""
import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

V = 2                                               # the sidecar's agent-surface version this reduction understands
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,20}")
OSI = re.compile(r"[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}", re.ASCII)
WORD = re.compile(r"[a-z][a-z0-9_]{0,31}")
ERRWORD = re.compile(r"[A-Za-z_]{1,40}")
NUM = re.compile(r"-?\d{1,15}(\.\d{1,10})?", re.ASCII)
DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
ASOF = re.compile(r"[0-9T:+\-.Z]{10,40}")

SIDES = frozenset({"BUY", "SELL"})
ASSETS = frozenset({"EQUITY", "OPTION"})
ORDER_TYPES = frozenset({"LIMIT", "MARKET", "STOP", "STOP_LIMIT"})
ORDER_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED", "CANCELLED", "QUEUED_CANCELLED", "FILLED", "REJECTED",
                            "PENDING_REPLACE", "PENDING_CANCEL", "EXPIRED", "REPLACED", "UNKNOWN"})
OPEN_CLOSE = frozenset({"OPEN", "CLOSE"})
ACCOUNT_TYPES = frozenset({"BROKERAGE", "HIGH_YIELD", "BOND_ACCOUNT", "RIA_ASSET", "TREASURY", "TRADITIONAL_IRA",
                           "ROTH_IRA", "ENTITY", "JOINT", "OTHER"})
PERMISSIONS = frozenset({"BUY_AND_SELL", "RESTRICTED_SETTLED_FUNDS_ONLY", "RESTRICTED_CLOSE_ONLY",
                         "RESTRICTED_NO_TRADING"})
OPTION_LEVELS = frozenset({"NONE", "LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4"})
TICKET_STATES = frozenset({"previewed", "sending", "placed", "unknown", "unexpected", "rejected", "refused",
                           "discarded", "expired", "cancelled", "filled", "ended"})
PURPOSES = frozenset({"csp", "cc", "close", "rollclose", "rollopen"})
LEG_STATUSES = frozenset({"open", "unrecorded", "unknown"})
TESTED = frozenset({"delta", "dte"})
ROLL_STATES = frozenset({"staged", "closing", "closed", "opening", "done", "abandoned", "stranded", "broken"})
BASIS_STATES = frozenset({"KNOWN", "UNKNOWN", "NOSHARES"})
WASH_FLAGS = frozenset({"CLEAR", "POSSIBLE", "UNKNOWN"})
WASH_MODES = frozenset({"warn", "refuse", "off"})
SECTIONS = ("account", "balances", "positions", "orders")
SUMMARY_KEYS = ("cycles_open", "legs_open", "basis_unknown", "rolls_active", "rolls_stuck")

# Every dollar key this view can carry. Stream mode removes each of them (and the whole balances block).
DOLLARS = frozenset({"cost_basis", "market_value", "unrealized_pl", "price", "limit_price", "avg_price", "equity",
                     "cash", "buying_power", "cash_only_buying_power", "options_buying_power", "basis"})
MAX_ROWS = 200                                      # rows kept per list; the digest and the panel show fewer


# ---- validators ------------------------------------------------------------------------------------------------------
def num(v: Any) -> Optional[str]:
    """A canonical decimal string, or None. Accepts an int, a finite Decimal or a decimal string; never a float."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        s = str(v)
    elif isinstance(v, Decimal):
        if not v.is_finite():
            return None
        s = format(v, "f")
    elif isinstance(v, str):
        s = v.strip()
    else:
        return None
    return s if NUM.fullmatch(s) else None


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 6 else None


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def _enum(v: Any, allowed: Iterable[str]) -> Optional[str]:
    return v if isinstance(v, str) and v in allowed else None


def symbol(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and SYMBOL.fullmatch(v) else None


def osi(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and OSI.fullmatch(v) else None


def instrument(v: Any) -> Optional[str]:
    return osi(v) or symbol(v)


def word(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and WORD.fullmatch(v) else None


def _day(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and DAY.fullmatch(v) else None


def _unknown(raw: Dict[str, Any], allowed: Iterable[str]) -> List[str]:
    got = raw.get("unknown")
    ok = set(allowed)
    return sorted({u for u in got if isinstance(u, str) and u in ok}) if isinstance(got, list) else []


def _row(raw: Any, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {}
    for key, check in fields.items():
        v = check(raw.get(key))
        if v is not None:
            out[key] = v
    out["unknown"] = _unknown(raw, fields)
    return out


ACCOUNT_FIELDS = {"type": lambda v: _enum(v, ACCOUNT_TYPES), "permissions": lambda v: _enum(v, PERMISSIONS),
                  "options_level": lambda v: _enum(v, OPTION_LEVELS)}
BALANCE_FIELDS = {k: num for k in ("equity", "cash", "buying_power", "cash_only_buying_power", "options_buying_power")}
POSITION_FIELDS = {"symbol": instrument, "asset": lambda v: _enum(v, ASSETS), "qty": num, "cost_basis": num,
                   "market_value": num, "unrealized_pl": num}
ORDER_FIELDS = {"symbol": instrument, "side": lambda v: _enum(v, SIDES), "type": lambda v: _enum(v, ORDER_TYPES),
                "status": lambda v: _enum(v, ORDER_STATUSES), "qty": num, "price": num, "filled_qty": num,
                "avg_price": num, "open_close": lambda v: _enum(v, OPEN_CLOSE)}
TICKET_FIELDS = {"state": lambda v: _enum(v, TICKET_STATES), "symbol": instrument, "side": lambda v: _enum(v, SIDES),
                 "asset": lambda v: _enum(v, ASSETS), "qty": num, "limit_price": num,
                 "open_close": lambda v: _enum(v, OPEN_CLOSE), "purpose": lambda v: _enum(v, PURPOSES)}


# ---- sections -------------------------------------------------------------------------------------------------------
def _rows(raw: Any, fields: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(raw, list):
        return None
    out = [r for r in (_row(x, fields) for x in raw[:MAX_ROWS]) if r is not None]
    # A row whose symbol could not be read stays only when the sidecar itself listed the symbol as unknown: it may hold a
    # claim on shares. A row with neither is dropped.
    return [r for r in out if "symbol" in r or "symbol" in r["unknown"]]


def _tickets(raw: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(raw, list):
        return None
    out = []
    for t in raw[:MAX_ROWS]:
        r = _row(t, TICKET_FIELDS)
        if r is None or "symbol" not in r:
            continue
        r.pop("unknown", None)
        r.setdefault("state", "unknown")
        out.append(r)
    return out


def _halt(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"halted": None, "cause": None}
    halted = _bool(raw.get("halted"))
    if halted is None:
        return {"halted": None, "cause": None}
    if not halted:
        return {"halted": False, "cause": None}
    return {"halted": True, "cause": word(raw.get("cause")) or "unknown"}


def _mode(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {k: _bool(raw.get(k)) for k in ("reads", "trading", "options")}


def _cycle(c: Any, stream: bool) -> Optional[Dict[str, Any]]:
    if not isinstance(c, dict):
        return None
    und = symbol(c.get("underlying"))
    if und is None:
        return None
    state = _enum(c.get("basis_state"), BASIS_STATES) or "UNKNOWN"
    out: Dict[str, Any] = {"underlying": und, "open": c.get("open") is True, "shares": num(c.get("shares")),
                           "basis_state": state, "legs": _int(c.get("legs"))}
    if state == "KNOWN" and not stream:
        b = num(c.get("basis"))
        if b is None:
            out["basis_state"] = "UNKNOWN"             # KNOWN with no readable number is not known
        else:
            out["basis"] = b
    return out


def _leg(lg: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(lg, dict):
        return None
    o, und = osi(lg.get("osi")), symbol(lg.get("underlying"))
    if o is None or und is None:
        return None
    out: Dict[str, Any] = {"osi": o, "underlying": und, "contracts": num(lg.get("contracts")),
                           "status": _enum(lg.get("status"), LEG_STATUSES) or "unknown", "dte": _int(lg.get("dte"))}
    t = lg.get("tested")
    if isinstance(t, list):
        out["tested"] = sorted({w for w in t if w in TESTED})
    return out


def _wash(r: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(r, dict):
        return None
    und = symbol(r.get("underlying"))
    if und is None:
        return None
    return {"underlying": und, "flag": _enum(r.get("washflag"), WASH_FLAGS) or "UNKNOWN",
            "window_start": _day(r.get("window_start")), "window_end": _day(r.get("window_end"))}


def _wheel(w: Any, stream: bool) -> Optional[Dict[str, Any]]:
    if not isinstance(w, dict):
        return None
    s = w.get("summary") if isinstance(w.get("summary"), dict) else {}
    summary: Dict[str, Any] = {k: _int(s.get(k)) for k in SUMMARY_KEYS}
    if "ledger" in s:
        summary["ledger"] = word(s.get("ledger")) or "unreadable"

    def lst(key, fn):
        raw = w.get(key)
        return None if not isinstance(raw, list) else [x for x in (fn(v) for v in raw[:MAX_ROWS]) if x is not None]
    rolls = w.get("rolls")
    return {"summary": summary,
            "cycles": lst("cycles", lambda c: _cycle(c, stream)),
            "legs": lst("legs", _leg),
            "rolls": None if not isinstance(rolls, list) else [
                _enum(r.get("state"), ROLL_STATES) or "unknown" for r in rolls[:MAX_ROWS] if isinstance(r, dict)],
            "wash_mode": _enum(w.get("wash_mode"), WASH_MODES),
            "wash": lst("wash", _wash)}


def _data(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    a = raw.get("as_of")
    unk = raw.get("unknown")
    err = raw.get("error")
    return {"polled": raw.get("polled") is True,
            "as_of": a if isinstance(a, str) and ASOF.fullmatch(a) else None,
            "unknown": [k for k in SECTIONS if isinstance(unk, list) and k in unk],
            "error": None if err is None else (err if isinstance(err, str) and ERRWORD.fullmatch(err) else "unreadable")}


# ---- the view ---------------------------------------------------------------------------------------------------------
def failed(code: str, stream: bool = False) -> Dict[str, Any]:
    """The view of a link that could not read the sidecar: every section None (UNKNOWN), a fixed code, no stale data."""
    out = {"enabled": True, "reachable": False, "code": code, "age_s": None, "stream": bool(stream),
           "mode": {"reads": None, "trading": None, "options": None}, "halt": {"halted": None, "cause": None},
           "account": None, "balances": None, "positions": None, "orders": None, "tickets": None, "wheel": None,
           "data": {"polled": False, "as_of": None, "unknown": list(SECTIONS), "error": None}}
    if stream:
        out.pop("balances")
    return out


def reduce(doc: Any, stream: bool = False) -> Optional[Dict[str, Any]]:
    """The allowlisted view of one sidecar document, or None when it is not a version-2 agent state at all."""
    if not isinstance(doc, dict) or doc.get("v") != V or isinstance(doc.get("v"), bool):
        return None
    acct = doc.get("account")
    bal = doc.get("balances")
    out: Dict[str, Any] = {
        "enabled": True, "reachable": True, "code": None, "age_s": None, "stream": bool(stream),
        "mode": _mode(doc.get("mode")),
        "halt": _halt(doc.get("halt")),
        "account": _row(acct, ACCOUNT_FIELDS) if isinstance(acct, dict) else None,
        "balances": _row(bal, BALANCE_FIELDS) if isinstance(bal, dict) else None,
        "positions": _rows(doc.get("positions"), POSITION_FIELDS),
        "orders": _rows(doc.get("orders"), ORDER_FIELDS),
        "tickets": _tickets(doc.get("tickets")),
        "wheel": _wheel(doc.get("wheel"), stream),
        "data": _data(doc.get("data")),
    }
    return strip(out) if stream else out


def strip(x: Any) -> Any:
    """Stream mode: drop every dollar key, recursively, the balances block whole, and dollar names from `unknown` lists."""
    if isinstance(x, dict):
        out = {k: strip(v) for k, v in x.items() if k not in DOLLARS and k != "balances"}
        if isinstance(out.get("unknown"), list):
            out["unknown"] = [u for u in out["unknown"] if u not in DOLLARS]
        return out
    if isinstance(x, list):
        return [strip(v) for v in x]
    return x
