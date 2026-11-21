"""Allowlisted reduction of the Public sidecar's /ai/state JSON (Round 36). Pure: no I/O, no float, never raises.

The sidecar already allowlists its own surface; this module does it again on our side, because a link is only as safe as
the end that reads it. Every field below is one of:
  * a number, kept as a canonical decimal STRING (the JSON is parsed with parse_float=Decimal, so a float never exists);
  * an enum from a fixed set copied from the sidecar's vocabulary, or a short word matching a strict pattern;
  * a validated symbol (equity ticker or OSI option symbol), or a YYYY-MM-DD day.
Nothing else crosses: no account number, no ticket id, no order reference, no confirm hash, no proposal id, no ledger event
id, no free text. Words that could carry text (halt cause, error, ledger code) are FIXED ENUMS copied from the Public desk's
own code; anything else reads `unreadable`. A field that fails its check is dropped; a section the sidecar could not read
stays None (UNKNOWN), never an empty list; a ROW that fails its check is never dropped silently: it is counted in
`incomplete[section]["unreadable"]`, and a list cut at MAX_ROWS is flagged `more`, so an unreadable short put can never
read as "no positions". Numbers are bounded BEFORE they are formatted (a huge exponent is never expanded). Stream mode drops
every dollar key, recursively.
"""
import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

V = 2                                               # the sidecar's agent-surface version this reduction understands
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,20}")
OSI = re.compile(r"[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}", re.ASCII)
WORD = re.compile(r"[a-z][a-z0-9_]{0,31}")
ERRWORD = re.compile(r"[A-Za-z_]{1,40}")
NUM = re.compile(r"-?\d{1,15}(\.\d{1,12})?", re.ASCII)
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

# Closed vocabularies copied from the Public desk (/home/mph/pxdev/public-desk at d9c73c6). Anything else is `unreadable`.
# Halt causes: desk/guard.py H_SEND_UNKNOWN, H_CANCEL_UNKNOWN, H_UNEXPECTED, H_REJECTED, H_BAD_FILE; wheel/desk.py trip_halt
# ("wheelfile") and _strand ("rollstranded", "rollpartial"); "unknown" is what its halt_cause() returns for an unreadable file.
HALT_CAUSES = frozenset({"sendunknown", "cancelunknown", "unexpected", "rejected", "badfile", "wheelfile", "rollstranded",
                         "rollpartial", "unknown"})
# Poll errors: errors.py PublicError subclasses' `code`, desk/reads.py Unknown ("account_unknown", "account_unflagged"),
# desk/state.py Runtime ("bad_reply").
ERRORS = frozenset({"PublicError", "PublicNotConfigured", "PublicAuthError", "PublicRateLimited", "PublicBadReply",
                    "account_unknown", "account_unflagged", "bad_reply"})
# Ledger and refusal codes: desk/guard.py CODES and wheel/rules.py CODES (the digest's `_code` passes only these).
LEDGER_CODES = frozenset({
    "disabled", "halted", "nocap", "qtycap", "notionalcap", "closed", "busy", "noaccount", "restricted", "stale",
    "unreadable", "noposition", "oversell", "nofunds", "badintent", "noticket", "used", "expired", "mismatch", "unwritable",
    "badstate", "noorder", "unsafe", "unconfirmed", "optionsoff", "optionlevel", "naked", "nocash", "noshort", "lossclose",
    "wheelonly",
    "noquote", "wide", "nodelta", "delta", "nooi", "thin", "dte", "capped", "noshares", "nobasis", "belowbasis", "debitroll",
    "noroll", "notested", "noprofit", "tick", "noleg", "ledger", "noproposal", "rollstate", "badrecord", "duplicate",
    "badrequest", "unrecorded", "washsale"})
# Number bounds, checked on the Decimal's digits and exponent BEFORE any formatting.
MAX_DIGITS = 18
MAX_ADJUSTED = 14
MIN_ADJUSTED = -12
MAX_INT = 10 ** 15

# Every dollar key this view can carry. Stream mode removes each of them (and the whole balances block).
DOLLARS = frozenset({"cost_basis", "market_value", "unrealized_pl", "price", "limit_price", "avg_price", "equity",
                     "cash", "buying_power", "cash_only_buying_power", "options_buying_power", "basis"})
MAX_ROWS = 200                                      # rows kept per list; the digest and the panel show fewer


# ---- validators ------------------------------------------------------------------------------------------------------
def num(v: Any) -> Optional[str]:
    """A canonical decimal string, or None. Accepts an int, a finite Decimal or a decimal string; never a float. A Decimal is
    judged on its digits and exponent first: one with more than MAX_DIGITS significant digits, or a magnitude outside
    10**MIN_ADJUSTED to 10**MAX_ADJUSTED (zero excepted), is refused without ever being expanded."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, int):
            if not -MAX_INT < v < MAX_INT:
                return None
            s = str(v)
        elif isinstance(v, Decimal):
            if not v.is_finite():
                return None
            t = v.as_tuple()
            if len(t.digits) > MAX_DIGITS or not isinstance(t.exponent, int) or t.exponent < -MAX_DIGITS:
                return None
            if v != 0 and not MIN_ADJUSTED <= v.adjusted() <= MAX_ADJUSTED:
                return None
            s = format(v, "f")
        elif isinstance(v, str):
            s = v.strip() if len(v) <= 40 else ""
        else:
            return None
    except (ValueError, ArithmeticError, MemoryError):
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
def _collect(raw: Any, fn, name: str, inc: Dict[str, Dict[str, Any]]) -> Optional[list]:
    """Reduce one list. None when the sidecar sent no list (UNKNOWN). Every row `fn` cannot read is COUNTED in
    inc[name]["unreadable"], never dropped silently, and a list longer than MAX_ROWS is flagged `more`."""
    if not isinstance(raw, list):
        return None
    out, bad = [], 0
    for x in raw[:MAX_ROWS]:
        r = fn(x)
        if r is None:
            bad += 1
        else:
            out.append(r)
    if bad or len(raw) > MAX_ROWS:
        inc[name] = {"unreadable": bad, "more": len(raw) > MAX_ROWS}
    return out


def _listed(fields: Dict[str, Any]):
    def fn(x: Any) -> Optional[Dict[str, Any]]:
        r = _row(x, fields)
        # A row whose symbol could not be read is kept (printed sym=UNKNOWN) only when the sidecar itself listed the symbol
        # as unknown; any other unreadable symbol (an OCC-padded option, BRK/B) makes the row unreadable, and counted.
        return r if r is not None and ("symbol" in r or "symbol" in r["unknown"]) else None
    return fn


def _ticket(t: Any) -> Optional[Dict[str, Any]]:
    r = _row(t, TICKET_FIELDS)
    if r is None or "symbol" not in r:
        return None
    r.pop("unknown", None)
    r.setdefault("state", "unknown")
    return r


def _halt(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"halted": None, "cause": None}
    halted = _bool(raw.get("halted"))
    if halted is None:
        return {"halted": None, "cause": None}
    if not halted:
        return {"halted": False, "cause": None}
    c = raw.get("cause")
    return {"halted": True, "cause": c if isinstance(c, str) and c in HALT_CAUSES else "unreadable"}


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


def _wheel(w: Any, stream: bool, inc: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(w, dict):
        return None
    s = w.get("summary") if isinstance(w.get("summary"), dict) else {}
    summary: Dict[str, Any] = {k: _int(s.get(k)) for k in SUMMARY_KEYS}
    if "ledger" in s:
        lg = s.get("ledger")
        summary["ledger"] = lg if isinstance(lg, str) and lg in LEDGER_CODES else "unreadable"

    return {"summary": summary,
            "cycles": _collect(w.get("cycles"), lambda c: _cycle(c, stream), "cycles", inc),
            "legs": _collect(w.get("legs"), _leg, "legs", inc),
            "rolls": _collect(w.get("rolls"), _roll, "rolls", inc),
            "wash_mode": _enum(w.get("wash_mode"), WASH_MODES),
            "wash": _collect(w.get("wash"), _wash, "wash", inc)}


def _roll(r: Any) -> Optional[str]:
    return (_enum(r.get("state"), ROLL_STATES) or "unknown") if isinstance(r, dict) else None


def _data(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    a = raw.get("as_of")
    unk = raw.get("unknown")
    err = raw.get("error")
    return {"polled": raw.get("polled") is True,
            "as_of": a if isinstance(a, str) and ASOF.fullmatch(a) else None,
            "unknown": [k for k in SECTIONS if isinstance(unk, list) and k in unk],
            "error": None if err is None else (err if isinstance(err, str) and err in ERRORS else "unreadable")}


# ---- the view ---------------------------------------------------------------------------------------------------------
def failed(code: str, stream: bool = False) -> Dict[str, Any]:
    """The view of a link that could not read the sidecar: every section None (UNKNOWN), a fixed code, no stale data."""
    out = {"enabled": True, "reachable": False, "code": code, "age_s": None, "stream": bool(stream),
           "mode": {"reads": None, "trading": None, "options": None}, "halt": {"halted": None, "cause": None},
           "account": None, "balances": None, "positions": None, "orders": None, "tickets": None, "wheel": None,
           "data": {"polled": False, "as_of": None, "unknown": list(SECTIONS), "error": None}, "incomplete": {}}
    if stream:
        out.pop("balances")
    return out


def reduce(doc: Any, stream: bool = False) -> Optional[Dict[str, Any]]:
    """The allowlisted view of one sidecar document, or None when it is not a version-2 agent state at all."""
    if not isinstance(doc, dict) or doc.get("v") != V or isinstance(doc.get("v"), bool):
        return None
    acct = doc.get("account")
    bal = doc.get("balances")
    inc: Dict[str, Dict[str, Any]] = {}
    out: Dict[str, Any] = {
        "enabled": True, "reachable": True, "code": None, "age_s": None, "stream": bool(stream),
        "mode": _mode(doc.get("mode")),
        "halt": _halt(doc.get("halt")),
        "account": _row(acct, ACCOUNT_FIELDS) if isinstance(acct, dict) else None,
        "balances": _row(bal, BALANCE_FIELDS) if isinstance(bal, dict) else None,
        "positions": _collect(doc.get("positions"), _listed(POSITION_FIELDS), "positions", inc),
        "orders": _collect(doc.get("orders"), _listed(ORDER_FIELDS), "orders", inc),
        "tickets": _collect(doc.get("tickets"), _ticket, "tickets", inc),
        "wheel": _wheel(doc.get("wheel"), stream, inc),
        "data": _data(doc.get("data")),
    }
    out["incomplete"] = inc
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
