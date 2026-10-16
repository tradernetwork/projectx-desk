from datetime import datetime, timezone

from projectx_desk.tradeday import session_start, trade_date

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


def test_parse_ts_accepts_any_fraction_length_z_and_offsets_and_naive():
    from projectx_desk.tradeday import parse_ts
    base = datetime(2026, 1, 14, 22, 59, 5, tzinfo=timezone.utc)
    for text, micro in (("2026-01-14T22:59:05Z", 0), ("2026-01-14T22:59:05.1Z", 100000), ("2026-01-14T22:59:05.12Z", 120000),
                        ("2026-01-14T22:59:05.123Z", 123000), ("2026-01-14T22:59:05.12345Z", 123450),
                        ("2026-01-14T22:59:05.1234567Z", 123456), ("2026-01-14T22:59:05.1234567890+00:00", 123456),
                        ("2026-01-14T22:59:05.5", 500000), ("2026-01-14T16:59:05.25-06:00", 250000)):
        got = parse_ts(text)
        assert got.tzinfo is not None and got.replace(microsecond=0) == base and got.microsecond == micro, text


def test_a_fill_with_an_odd_fraction_still_counts_toward_pnl_and_the_streak():
    from projectx_desk.state import LiveState
    s = LiveState()
    s.select_account(7)
    now = datetime(2026, 1, 14, 20, 0, tzinfo=timezone.utc)
    s.seed_trades([{"id": 1, "accountId": 7, "creationTimestamp": "2026-01-14T19:30:00.12Z", "profitAndLoss": -20.0, "fees": 0},
                   {"id": 2, "accountId": 7, "creationTimestamp": "2026-01-14T19:40:00.12345Z", "profitAndLoss": -10.0, "fees": 0}])
    assert s.day_pnl(now) == -30.0 and [round(p) for _, p in s.closed_trades(now)] == [-20, -10]
