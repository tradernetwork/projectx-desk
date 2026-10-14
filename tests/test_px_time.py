from datetime import datetime, timezone

from px_time import session_start, trade_date

U = timezone.utc


def test_before_and_after_the_1700_ct_boundary_winter():
    # Jan: CT = UTC-6. 16:59 CT = 22:59Z (same session), 17:00 CT = 23:00Z (next trade date).
    assert trade_date(datetime(2026, 1, 14, 22, 59, tzinfo=U)).isoformat() == "2026-01-14"
    assert trade_date(datetime(2026, 1, 14, 23, 0, tzinfo=U)).isoformat() == "2026-01-15"


def test_boundary_holds_across_dst():
    # Jul: CT = UTC-5. 17:00 CT = 22:00Z.
    assert trade_date(datetime(2026, 7, 14, 21, 59, tzinfo=U)).isoformat() == "2026-07-14"
    assert trade_date(datetime(2026, 7, 14, 22, 0, tzinfo=U)).isoformat() == "2026-07-15"


def test_utc_midnight_does_not_split_a_session():
    # 6:59pm CT Jan 14 and 7:01pm CT: UTC date flips at 18:00 CT in winter; both are one session.
    a = datetime(2026, 1, 15, 0, 59, tzinfo=U)   # 18:59 CT Jan 14
    b = datetime(2026, 1, 15, 1, 30, tzinfo=U)   # 19:30 CT Jan 14
    assert trade_date(a) == trade_date(b)


def test_session_start_is_most_recent_1700_ct():
    assert session_start(datetime(2026, 1, 14, 15, 0, tzinfo=U)) == datetime(2026, 1, 13, 23, 0, tzinfo=U)
    assert session_start(datetime(2026, 1, 14, 23, 30, tzinfo=U)) == datetime(2026, 1, 14, 23, 0, tzinfo=U)
