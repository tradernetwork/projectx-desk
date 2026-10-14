"""One definition of 'the trading day' for caps, P&L and the digest.

CME futures sessions run 17:00 CT to 17:00 CT. Bucketing by UTC or calendar date puts the
daily loss cap's rollover at the wrong hour (7 or 8 pm CT) and splits one session in two.
"""
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
