"""Session context: the opening range, the opening gap and the first break of that range. Pure: rows in, dicts out, no I/O.

CONTEXT ONLY. Never read by the guard or sizing, and it imports none of guard, client, hub, mcp or app (a test pins that). It describes
what the session did so far; it is not a signal and carries no rate or edge claim.

Clock: tradeday.py is the one definition of the trading day (17:00 CT roll) and of Central time. A bar stamped 17:00 CT or later belongs
to the NEXT trade date, so Sunday evening is Monday's session and the weekend never becomes a "prior close".

Bars are the /bars shape, oldest first: {"timestamp": epoch ms of the bar START, open, high, low, close}. A row with any non-finite or
missing price it needs is omitted; a missing value is never read as 0.
"""
import re
from datetime import datetime, timedelta, timezone

from .tradeday import CT, trade_date

OPEN_CT = "08:30"
OR_MINUTES = 15
# Descriptive thresholds, not tuned and not an edge claim: a gap of at least GAP_PCT percent of the prior close is called UP or DOWN,
# anything smaller FLAT. They only bucket the number for reading; nothing trades on them.
GAP_PCT = 0.25

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _local(ts):
    if not _fin(ts):
        return None
    try:
        return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).astimezone(CT)
    except (OverflowError, OSError, ValueError):
        return None


def _rows(bars, *fields):
    """Rows with a finite timestamp and finite `fields`, as (local CT datetime, row)."""
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        dt = _local(b.get("timestamp"))
        if dt is not None and all(_fin(b.get(f)) for f in fields):
            out.append((dt, b))
    return out


def _hhmm(text):
    m = _HHMM.match(text) if isinstance(text, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def opening_range(bars, open_ct=OPEN_CT, minutes=OR_MINUTES):
    """High/low/open of the bars STARTING in [open_ct, open_ct + minutes) CT on the latest trade date in `bars`, else None.

    `complete` is True only when the first bar of the window starts exactly at the open (no partial first bar) and a bar at or after the
    window end exists, so the range cannot still be forming. An incomplete range is still reported, flagged, never padded.
    """
    hm = _hhmm(open_ct)
    if hm is None or not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        return None
    allrows = _rows(bars, "open", "high", "low", "close")
    if not allrows:
        return None
    day = max(trade_date(dt) for dt, _ in allrows)
    start = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=CT)
    end = start + timedelta(minutes=minutes)
    inwin = sorted(((dt, b) for dt, b in allrows if start <= dt < end), key=lambda r: r[0])
    if not inwin:
        return None
    later = any(dt >= end and trade_date(dt) == day for dt, _ in allrows)
    return {"trade_date": day.isoformat(), "high": max(b["high"] for _, b in inwin), "low": min(b["low"] for _, b in inwin),
            "open": inwin[0][1]["open"], "bars": len(inwin), "complete": bool(inwin[0][0] == start and later),
            "start_ms": int(start.timestamp() * 1000), "end_ms": int(end.timestamp() * 1000)}


def gap_pct(today_open, prev_close):
    """Opening gap in PERCENT of the prior close (+1.0 is one percent up). None unless both prices are finite and positive."""
    if not _fin(today_open) or not _fin(prev_close) or today_open <= 0 or prev_close <= 0:
        return None
    return (today_open - prev_close) / prev_close * 100.0


def gap_class(pct):
    """UP, DOWN or FLAT against +/-GAP_PCT; NA when the gap is unknown."""
    if not _fin(pct):
        return "NA"
    return "UP" if pct >= GAP_PCT else "DOWN" if pct <= -GAP_PCT else "FLAT"


def first_break(bars, or_high, or_low, after_ms=None):
    """The first bar CLOSING beyond the range: {"side": "UP"|"DOWN", "ts": bar start ms}, or None. Bars before `after_ms` are skipped
    (pass the window end so the range's own bars cannot count). Rows without a finite close are omitted."""
    if not _fin(or_high) or not _fin(or_low):
        return None
    for dt, b in sorted(_rows(bars, "close"), key=lambda r: r[1]["timestamp"]):
        ts = b["timestamp"]
        if _fin(after_ms) and ts < after_ms:
            continue
        if b["close"] > or_high:
            return {"side": "UP", "ts": ts}
        if b["close"] < or_low:
            return {"side": "DOWN", "ts": ts}
    return None


def context(bars, open_ct=OPEN_CT, minutes=OR_MINUTES):
    """One dict for the digest: the opening range, the gap (the first bar at or after the open versus the close of the last bar of an EARLIER
    trade date) and the first break after the range. None when there are no usable bars. Parts that cannot be known are None."""
    rng = opening_range(bars, open_ct, minutes)
    if rng is None:
        return None if not _rows(bars, "close") else {"or": None, "prev_close": None, "gap_pct": None, "gap_class": "NA", "break": None}
    rows = sorted(_rows(bars, "open", "close"), key=lambda r: r[1]["timestamp"])
    day = rng["trade_date"]
    prior = [b for dt, b in rows if trade_date(dt).isoformat() < day]
    prev_close = prior[-1]["close"] if prior else None
    pct = gap_pct(rng["open"], prev_close)
    brk = first_break(bars, rng["high"], rng["low"], rng["end_ms"]) if rng["complete"] else None
    return {"or": rng, "prev_close": prev_close, "gap_pct": None if pct is None else round(pct, 3), "gap_class": gap_class(pct), "break": brk}
