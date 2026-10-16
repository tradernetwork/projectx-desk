"""One definition of 'the trading day' for caps, P&L and the digest.

CME futures sessions run 17:00 CT to 17:00 CT. Bucketing by UTC or calendar date puts the
daily loss cap's rollover at the wrong hour (7 or 8 pm CT) and splits one session in two.
"""
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
SESSION_OPEN = time(17, 0)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def trade_date(dt) -> date:
    """Session date for a timestamp: 17:00 CT onward belongs to the NEXT calendar date."""
    local = _aware(dt).astimezone(CT)
    return local.date() + timedelta(days=1) if local.time() >= SESSION_OPEN else local.date()


def session_start(now=None) -> datetime:
    """UTC start of the session containing `now` (the most recent 17:00 CT)."""
    local = _aware(now or datetime.now(timezone.utc)).astimezone(CT)
    start = datetime.combine(local.date(), SESSION_OPEN, tzinfo=CT)
    if local < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


_FRAC = re.compile(r"\.(\d+)")


def parse_ts(text) -> datetime:
    """Parse a gateway timestamp on any supported Python: 'Z' or an offset, and 1 to 9 fractional digits.

    Python 3.10's fromisoformat only accepts 3 or 6 fractional digits, and an unparseable fill used to be skipped
    silently, which would drop a LOSS from the day's P&L, the cooldown and the lockout. Naive times are UTC.
    """
    s = str(text).strip().replace("Z", "+00:00")
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
