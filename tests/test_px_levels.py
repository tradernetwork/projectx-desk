import math

import pytest

from projectx_desk import sizing
from projectx_desk.levels import atr_series, bounce_stats, ema_series, ema_stack, percentile_rank


def test_ema_is_sma_seeded_and_matches_a_hand_calc():
    e = ema_series([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2.0
    assert e[3] == pytest.approx(4 * 0.5 + 2.0 * 0.5) and e[4] == pytest.approx(5 * 0.5 + 3.0 * 0.5)
    assert ema_series([1, 2], 5) == [None, None]
    with pytest.raises(ValueError):
        ema_series([1], 0)


def test_atr_series_matches_sizing_atr_at_the_end():
    h = [10 + (i % 5) + i * 0.1 for i in range(60)]
    l = [x - 2 - (i % 3) * 0.2 for i, x in enumerate(h)]
    c = [(a + b) / 2 for a, b in zip(h, l)]
    s = atr_series(h, l, c, 14)
    assert s[:14] == [None] * 14 and s[14] is not None
    assert s[-1] == pytest.approx(sizing.atr(h, l, c, 14))


def test_percentile_rank():
    series = list(range(1, 101))
    assert percentile_rank(80, series) == 80.0 and percentile_rank(100, series) == 100.0
    assert percentile_rank(5, series[:10]) is None          # too little history
    assert percentile_rank(None, series) is None


def test_ema_stack_states():
    up = [100 + i * 0.5 for i in range(80)]
    down = [200 - i * 0.5 for i in range(80)]
    chop = [100 + i * 0.5 for i in range(60)] + [129.5 - (j + 1) * 2.0 for j in range(8)]   # uptrend that just rolled over: tangled stack
    assert ema_stack(up)["state"] == "BULL"
    assert ema_stack(down)["state"] == "BEAR"
    assert ema_stack(chop)["state"] == "MIXED"
    assert ema_stack([1, 2, 3])["state"] == "NA"


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c, "t": 0}


FLAT_ATR = [1.0] * 40


def test_support_bounce_is_counted():
    level = [100.0] * 40
    bars = [bar(105, 106, 104, 105) for _ in range(10)]
    bars += [bar(105, 105, 100.1, 102)]                 # i=10 touches 100 from above
    bars += [bar(102, 103.5, 101.8, 103)] + [bar(103, 104, 102.5, 103)] * 5 + [bar(103, 104, 102.5, 103)] * 10
    r = bounce_stats(bars, level, FLAT_ATR[:len(bars)] if len(bars) <= 40 else [1.0] * len(bars))
    assert r["n"] == 1 and r["bounced"] == 1 and r["failed"] == 0 and r["rate"] == 100.0


def test_failure_through_the_level_is_counted_and_clusters_collapse():
    level = [100.0] * 40
    bars = [bar(105, 106, 104, 105)] * 10
    bars += [bar(105, 105, 100.0, 100.1), bar(100.1, 100.4, 99.9, 100.0), bar(100, 100.3, 99.5, 99.8)]   # grind: ONE event
    bars += [bar(99.8, 99.9, 99.0, 99.2)] * 15                                                          # then closes through
    r = bounce_stats(bars, level, [1.0] * len(bars))
    assert r["n"] == 1 and r["failed"] == 1 and r["bounced"] == 0
    assert r["last_result"] == "FAIL"


def test_resistance_bounce_down_and_pending_is_not_counted():
    level = [100.0] * 30
    bars = [bar(95, 96, 94, 95)] * 10 + [bar(95, 99.95, 94.8, 96)]          # touches 100 from below at i=10
    bars += [bar(99.5, 99.6, 99.4, 99.5)] * 3                                # hovers just under it: 3 bars, no verdict yet
    r = bounce_stats(bars, level, [1.0] * len(bars))
    assert r["n"] == 0 and r["pending"] == 1 and r["rate"] is None
    bars += [bar(99, 99.2, 94.9, 95)] * 12                                    # then drops >= 0.75 ATR away
    r2 = bounce_stats(bars, level, [1.0] * len(bars))
    assert r2["n"] == 1 and r2["bounced"] == 1 and r2["events"][0]["kind"] == "RESISTANCE"


def test_nothing_happens_is_neutral_and_none_levels_are_skipped():
    level = [None] * 5 + [100.0] * 25
    bars = [bar(105, 106, 104, 105)] * 10 + [bar(105, 105, 100.1, 101.0)] + [bar(100.5, 100.7, 100.3, 100.5)] * 15
    r = bounce_stats(bars, level, [1.0] * len(bars))
    assert r["n"] == 1 and r["neutral"] == 1 and r["bounced"] == 0 and r["failed"] == 0


def test_risk_scale_only_ever_reduces_and_says_why():
    full = sizing.risk_scale(30.0, 50.0)
    assert full["multiplier"] == 1.0 and not full["reduced"]
    chop = sizing.risk_scale(15.0, 50.0)
    assert chop["multiplier"] == 0.67 and chop["reduced"] and "chop" in chop["reasons"][0]
    vol = sizing.risk_scale(30.0, 90.0)
    assert vol["multiplier"] == 0.67 and "90th percentile" in vol["reasons"][1]
    both = sizing.risk_scale(15.0, 90.0)
    assert both["multiplier"] == pytest.approx(0.67 * 0.67, abs=1e-3)
    unknown = sizing.risk_scale(None, None)
    assert unknown["multiplier"] == pytest.approx(0.5625) and "unknown" in unknown["reasons"][0]
    assert sizing.risk_scale(float("nan"), 50.0)["reduced"]
    for adx in (None, 5, 19.9, 20, 40, 80):
        for p in (None, 0, 50, 80, 80.1, 99):
            assert 0.25 <= sizing.risk_scale(adx, p)["multiplier"] <= 1.0


def test_percentile_ordinals_read_correctly():
    from projectx_desk.sizing import _ordinal
    assert [_ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 32, 63, 80, 92, 100)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "32nd", "63rd", "80th", "92nd", "100th"]
    assert "32nd percentile" in sizing.risk_scale(30.0, 32.0)["reasons"][1]


from projectx_desk.levels import placebo_rate, verdict


def test_placebo_uses_same_rules_on_shifted_lines_and_sample_size_is_pooled():
    import random
    random.seed(5)
    price, bars = 100.0, []
    for i in range(300):
        n = price + random.uniform(-0.8, 0.8)
        bars.append(bar(price, max(price, n) + 0.2, min(price, n) - 0.2, n))
        price = n
    level = [100.0] * 300
    atr = [1.0] * 300
    pl = placebo_rate(bars, level, atr)
    assert pl["n"] > 0 and 0 <= pl["rate"] <= 100
    per = [bounce_stats(bars, [100.0 + k] * 300, atr) for k in (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5)]
    assert pl["n"] == sum(x["n"] for x in per) and pl["bounced"] == sum(x["bounced"] for x in per)
    assert placebo_rate(bars, [None] * 300, atr) == {"n": 0, "bounced": 0, "rate": None}


def test_verdict_is_conservative():
    assert verdict({"n": 4, "rate": 100.0}, {"n": 200, "rate": 50.0}) == "TOO_FEW_TESTS"              # tiny real sample
    assert verdict({"n": 20, "rate": 90.0}, {"n": 10, "rate": 50.0}) == "TOO_FEW_TESTS"               # tiny control sample
    assert verdict({"n": 20, "rate": 80.0}, {"n": 200, "rate": 60.0}) == "TOO_FEW_TESTS"               # 20 tests: a 20pt gap is still 1.8 SE
    assert verdict({"n": 40, "rate": 70.0}, {"n": 200, "rate": 60.0}) == "NOT_DISTINGUISHABLE"        # 10pt gap is not enough
    assert verdict({"n": 40, "rate": 80.0}, {"n": 200, "rate": 60.0}) == "ABOVE_RANDOM"
    assert verdict({"n": 40, "rate": 30.0}, {"n": 200, "rate": 60.0}) == "BELOW_RANDOM"
    assert verdict({"n": 40, "rate": None}, {"n": 200, "rate": 60.0}) == "TOO_FEW_TESTS"
    assert verdict({"n": 20, "rate": 80.0}, {"n": 200, "rate": 60.0}, min_n=8) == "ABOVE_RANDOM"      # the old gate, on request only


def test_a_level_that_is_just_a_random_neighbour_is_not_distinguished_from_random():
    import random
    random.seed(11)
    price, bars = 100.0, []
    for i in range(400):
        n = price + random.uniform(-0.8, 0.8)
        bars.append(bar(price, max(price, n) + 0.2, min(price, n) - 0.2, n))
        price = n
    level = [100.0] * 400                                    # an arbitrary horizontal line in a random walk
    atr = [1.0] * 400
    real = bounce_stats(bars, level, atr)
    pl = placebo_rate(bars, level, atr)
    assert verdict({"n": real["n"], "rate": real["rate"]}, pl) in ("NOT_DISTINGUISHABLE", "TOO_FEW_TESTS", "BELOW_RANDOM")


def test_binomial_tail_is_exact_and_clamped():
    from math import comb
    from projectx_desk.levels import binom_sf
    n, p = 30, 0.45
    exact = sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(22, n + 1))
    assert binom_sf(22, n, p) == pytest.approx(exact, rel=1e-9)
    assert binom_sf(0, 30, 0.5) == 1.0 and binom_sf(5, 0, 0.5) == 1.0 and binom_sf(31, 30, 0.5) == 0.0
    assert binom_sf(10, 30, 1.2) == 1.0 and binom_sf(10, 30, -0.1) == 0.0          # calibrated placebo can leave [0, 1]
    assert binom_sf(1, 30, 0.5) == pytest.approx(1 - 0.5 ** 30)
    # the reviewer's arithmetic: 8 tests, 60% vs 45%: about one in five clears a 15-point gap by luck
    assert 0.15 < binom_sf(5, 8, 0.45) < 0.30
    assert binom_sf(18, 30, 0.45) < 0.10 and binom_sf(19, 30, 0.45) < 0.05            # 30 tests at the same gap: not luck


def test_benjamini_hochberg_controls_the_family_and_skips_unknowns():
    from projectx_desk.levels import benjamini_hochberg as bh
    assert bh([]) == [] and bh([None, float("nan")]) == [None, None]
    # one real signal among 124 null p-values: it survives, the nulls do not
    ps = [0.0001] + [0.3 + 0.005 * i for i in range(124)]
    r = bh(ps, 0.10)
    assert r[0] is True and not any(r[1:])
    # a p-value that beats 0.05 alone does NOT survive against 124 siblings
    assert bh([0.04] + [0.5] * 124, 0.10)[0] is False
    # step-up: a p above its own threshold still passes when a larger p below it does
    assert bh([0.01, 0.02, 0.06, 0.9], 0.10) == [True, True, True, False]
    assert bh([0.5, None, 0.001], 0.10) == [False, None, True]
    assert bh([0.001], 0.0) == [None] and bh([0.001], 1.0) == [None]             # q outside (0, 1): nothing is decided
