"""session.py: opening range, gap and first break, pure. Times are CT via tradeday; 08:30 CT is the cash open."""
import ast
import pathlib
import re
from datetime import datetime

from projectx_desk import session as S
from projectx_desk.tradeday import CT

ROOT = pathlib.Path(__file__).resolve().parent.parent


def ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=CT).timestamp() * 1000)


def bar(t, o, h, l, c):
    return {"timestamp": t, "open": o, "high": h, "low": l, "close": c, "volume": 10}


def five_min(y, mo, d, h, mi, n, start_px=100.0, step=0.0):
    """n consecutive 5m bars from h:mi CT, each a 1-point range around a drifting price."""
    out, t = [], ms(y, mo, d, h, mi)
    for i in range(n):
        p = start_px + step * i
        out.append(bar(t + i * 300_000, p, p + 1, p - 1, p + 0.5))
    return out


# 2026-11-17 is a Tuesday
def test_opening_range_uses_only_bars_from_0830_for_15_minutes_and_ignores_the_overnight():
    bars = five_min(2026, 11, 17, 8, 15, 2, 50.0) + five_min(2026, 11, 17, 8, 30, 4, 100.0, 1.0)   # 08:15, 08:20 | 08:30, 08:35, 08:40, 08:45
    r = S.opening_range(bars)
    assert (r["high"], r["low"], r["open"]) == (103.0, 99.0, 100.0)    # 08:30, 08:35, 08:40 only; the 08:45 bar is outside
    assert r["bars"] == 3 and r["complete"] is True


def test_opening_range_is_forming_until_a_bar_at_or_after_the_window_end_exists():
    r = S.opening_range(five_min(2026, 11, 17, 8, 30, 2, 100.0))
    assert r["complete"] is False and r["bars"] == 2


def test_a_partial_first_bar_is_flagged_not_completed():
    bars = five_min(2026, 11, 17, 8, 32, 4, 100.0)                        # data starts at 08:32: the 08:30 bar is missing
    r = S.opening_range(bars)
    assert r["complete"] is False and r["high"] == 101.0 and r["bars"] == 3


def test_opening_range_on_an_empty_list_and_with_non_finite_values():
    assert S.opening_range([]) is None and S.opening_range(None) is None
    bars = five_min(2026, 11, 17, 8, 30, 4, 100.0)
    bars[1]["high"] = float("nan")
    bars[2]["low"] = None
    r = S.opening_range(bars)
    assert r["high"] == 101.0 and r["low"] == 99.0 and r["bars"] == 1      # the bad rows are omitted, never read as 0
    assert S.opening_range([bar(ms(2026, 11, 17, 8, 30), float("inf"), float("nan"), float("nan"), 1)]) is None


def test_opening_range_picks_the_latest_trading_day_and_a_bar_after_1700_belongs_to_the_next_day():
    mon = five_min(2026, 11, 16, 8, 30, 4, 10.0)
    sun_night_then_tue = five_min(2026, 11, 16, 17, 5, 2, 200.0) + five_min(2026, 11, 17, 8, 30, 4, 100.0)
    r = S.opening_range(mon + sun_night_then_tue)
    assert r["low"] == 99.0 and r["trade_date"] == "2026-11-17"


def test_gap_pct_and_class_are_descriptive_and_na_safe():
    assert S.gap_pct(101.0, 100.0) == 1.0
    assert S.gap_pct(None, 100.0) is None and S.gap_pct(100.0, 0) is None and S.gap_pct(float("nan"), 100.0) is None
    assert S.gap_class(0.5) == "UP" and S.gap_class(-0.5) == "DOWN" and S.gap_class(0.1) == "FLAT" and S.gap_class(None) == "NA"
    assert S.gap_class(S.GAP_PCT) == "UP" and S.gap_class(-S.GAP_PCT) == "DOWN"


def test_gap_across_the_1700_roll_compares_tuesdays_open_with_mondays_last_close():
    mon = five_min(2026, 11, 16, 15, 50, 3, 100.0)                         # last Monday bar 15:60.. closes 100.5
    mon_last_close = mon[-1]["close"]
    overnight = five_min(2026, 11, 16, 17, 5, 2, 90.0)                     # after the roll: Tuesday's trade day
    tue = five_min(2026, 11, 17, 8, 30, 4, 102.0)
    ctx = S.context(mon + overnight + tue)
    assert ctx["prev_close"] == mon_last_close                             # the overnight bars did NOT become 'prior close'
    assert ctx["gap_pct"] == round((102.0 - mon_last_close) / mon_last_close * 100, 3)
    assert ctx["gap_class"] == "UP"


def test_gap_over_a_weekend_uses_fridays_close():
    fri = five_min(2026, 11, 13, 15, 40, 4, 100.0)                         # Friday 2026-11-13
    sun = five_min(2026, 11, 15, 17, 5, 2, 95.0)                           # Sunday evening: Monday's trade day
    mon = five_min(2026, 11, 16, 8, 30, 4, 99.0)
    ctx = S.context(fri + sun + mon)
    assert ctx["prev_close"] == fri[-1]["close"] and ctx["gap_class"] == "DOWN"


def test_first_break_is_the_first_close_beyond_the_range_and_names_the_side():
    after = five_min(2026, 11, 17, 8, 45, 5, 100.0)
    after[1]["close"], after[3]["close"] = 105.0, 90.0
    r = S.first_break(after, 103.0, 99.0)
    assert r == {"side": "UP", "ts": after[1]["timestamp"]}
    after[1]["close"] = 101.0
    assert S.first_break(after, 103.0, 99.0) == {"side": "DOWN", "ts": after[3]["timestamp"]}
    assert S.first_break(after[:2], 103.0, 99.0) is None


def test_first_break_skips_bad_rows_and_bad_inputs_and_respects_after_ms():
    bars = five_min(2026, 11, 17, 8, 45, 3, 100.0)
    bars[0]["close"] = float("nan")
    bars[1]["close"] = 120.0
    assert S.first_break(bars, 103.0, 99.0)["ts"] == bars[1]["timestamp"]
    assert S.first_break(bars, float("nan"), 99.0) is None and S.first_break([], 1.0, 0.5) is None
    assert S.first_break(bars, 103.0, 99.0, after_ms=bars[2]["timestamp"]) is None


def test_context_full_line_inputs_and_empty():
    assert S.context([]) is None
    bars = five_min(2026, 11, 16, 15, 50, 3, 100.0) + five_min(2026, 11, 17, 8, 30, 5, 100.0)
    bars[-1]["close"] = 150.0
    c = S.context(bars)
    assert c["or"]["complete"] and c["break"]["side"] == "UP" and c["gap_class"] in ("UP", "DOWN", "FLAT")


def test_module_is_pure_and_cannot_reach_the_order_side():
    src = (ROOT / "projectx_desk" / "session.py").read_text()
    tree = ast.parse(src)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if re.search(r"guard|client|hub|mcp|app|httpx|requests|socket", m)}, imported
    assert not re.search(r"\.write\(|open\(|/api/Order|place", src)
    assert "context, never read by the guard or sizing" in src.lower() or "never read by the guard" in src.lower()
