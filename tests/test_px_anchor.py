"""px_anchor: level-anchored stop/target. Numbers worked by hand in comments."""
import math
import random

import pytest

from projectx_desk import anchor as A
# Common fixture: entry 7800.00, ATR 10, tick 0.25, tickValue 0.50 (MNQ-like).
#   D0 = ceil(1.5 x 10 / 0.25) = 60 ticks = 15.0 pts ; cap = 2.0 x 10 = 20.0 pts
E, ATR, TS, TV = 7800.0, 10.0, 0.25, 0.5


def lv(id_, price, tf="15m", verdict="ABOVE_RANDOM", **kw):
    d = {"id": id_, "tf": tf, "price": price, "verdict": verdict, "family": "keltner", "tests": 20,
         "hold_rate": 0.7, "placebo_rate": 0.5, "stable": True, "label": id_}
    d.update(kw)
    return d


def run(side="LONG", levels=(), entry=E, atr=ATR, ts=TS, tv=TV, **kw):
    return A.anchor_stop_target(side, entry, atr, ts, tv, list(levels), **kw)


def test_base_stop_no_levels():
    r = run()
    assert r.ok and r.stop_points == 15.0 and r.stop_price == 7785.0 and r.stop_ticks == 60
    assert r.k_eff == 1.5 and r.anchored_to is None and r.target_price is None and r.rr is None
    assert r.breakeven_win_rate is None and r.risk_per_contract == 30.0
    assert any(s.startswith("BASE 1.5xATR: no respected level between 1.5 and 2.0 ATR") for s in r.reasons)
    assert "TARGET none: no respected level >= 1R" in r.reasons


def test_anchor_long_happy_path():
    # level 7783.00 -> d=17 in [15,20]; buffer=max(0.5, 1.0)=1.0 -> stop 7782.00; dist 18; k_eff 1.8
    r = run(levels=[lv("kc_lo20", 7783.0)])
    assert r.stop_price == 7782.0 and r.stop_points == 18.0 and r.stop_ticks == 72
    assert r.k_eff == pytest.approx(1.8) and r.anchored_to == {"id": "kc_lo20", "tf": "15m"}
    assert r.reasons[0] == "ANCHORED beyond 15m kc_lo20 at 7783"
    assert r.risk_per_contract == 72 * 0.5


def test_anchor_short_mirror():
    # level 7817.00 above entry: d=17; stop 7818.00; dist 18
    r = run("SHORT", [lv("kc_up20", 7817.0)])
    assert r.stop_price == 7818.0 and r.stop_points == 18.0 and r.k_eff == pytest.approx(1.8)
    assert r.anchored_to == {"id": "kc_up20", "tf": "15m"}


def test_long_short_symmetry():
    ups = [lv("a", 7817.0), lv("b", 7740.0, tf="1h")]
    downs = [lv("a", 7783.0), lv("b", 7860.0, tf="1h")]
    rl, rs = run("LONG", downs), run("SHORT", ups)
    assert rl.stop_points == rs.stop_points and rl.stop_ticks == rs.stop_ticks
    assert E - rl.stop_price == rs.stop_price - E
    assert rl.target_points == rs.target_points == 60.0 and rl.rr == rs.rr


def test_level_inside_d0_ignored():
    # d=12 < D0=15: base stop already beyond it
    r = run(levels=[lv("x", 7788.0)])
    assert r.anchored_to is None and r.stop_points == 15.0


def test_level_exactly_at_d0_anchors_beyond():
    # d=15 == D0: allowed; buffer 1.0 -> dist 16
    r = run(levels=[lv("x", 7785.0)])
    assert r.anchored_to is not None and r.stop_points == 16.0


def test_level_beyond_cap_ignored():
    # d=21 > cap 20
    r = run(levels=[lv("x", 7779.0)])
    assert r.anchored_to is None and r.stop_points == 15.0 and r.stop_price == 7785.0


def test_level_in_range_but_buffer_breaks_cap_uses_base():
    # d=19.5 <= 20, but 19.5 + 1.0 buffer = 20.5 > cap -> do NOT anchor
    r = run(levels=[lv("x", 7780.5)])
    assert r.anchored_to is None and r.stop_points == 15.0
    assert "needs a stop wider than 2.0xATR" in r.reasons[0]


def test_nearest_of_several_qualifying():
    # d=16 (nearest), d=18, d=19 all qualify -> anchors to 7784.0: stop 7783.0 dist 17
    r = run(levels=[lv("far", 7781.0), lv("near", 7784.0, tf="5m"), lv("mid", 7782.0)])
    assert r.anchored_to == {"id": "near", "tf": "5m"} and r.stop_points == 17.0


def test_nearest_does_not_fall_through_to_farther_when_buffer_breaks_cap():
    # nearest qualifying d=19.5 breaks cap with buffer; farther one is wider still: base stop, no hop
    r = run(levels=[lv("n", 7780.5), lv("f", 7780.25)])
    assert r.anchored_to is None and r.stop_points == 15.0


def test_unrespected_verdicts_never_anchor_or_target():
    for v in ("NOT_DISTINGUISHABLE", "BELOW_RANDOM", "TOO_FEW_TESTS", "UNKNOWN", "RESPECTED", "REJECTED", None, 5):
        r = run(levels=[lv("s", 7783.0, verdict=v), lv("t", 7860.0, verdict=v)])
        assert r.anchored_to is None and r.stop_points == 15.0 and r.target_price is None, v


def test_gex_family_never_anchors():
    r = run(levels=[lv("gx", 7783.0, family="gex"), lv("gt", 7860.0, family="GEX")])
    assert r.anchored_to is None and r.target_price is None


def test_require_stable_option():
    unstable = lv("u", 7783.0, stable=False)
    assert run(levels=[unstable]).anchored_to is not None
    assert run(levels=[unstable], require_stable=True).anchored_to is None
    assert run(levels=[lv("n", 7783.0, stable=None)], require_stable=True).anchored_to is None


def test_target_at_least_1r():
    # stop 15 -> need >= 15 pts. 7810 (10) too close; 7815 (15) exactly 1R OK; 7830 farther
    r = run(levels=[lv("t1", 7810.0), lv("t2", 7815.0, tf="1h"), lv("t3", 7830.0)])
    assert r.target_price == 7815.0 and r.target_level == {"id": "t2", "tf": "1h"}
    assert r.rr == 1.0 and r.breakeven_win_rate == pytest.approx(50.0)


def test_target_none_when_all_under_1r_or_wrong_side():
    r = run(levels=[lv("t1", 7810.0), lv("t2", 7780.0)])   # 7780 is adverse; stays unanchored? d=20 qualifies
    assert r.target_price is None and r.rr is None and r.breakeven_win_rate is None and r.target_level is None


def test_target_uses_final_anchored_stop_distance():
    # anchored stop dist 18 -> target at +16 does not qualify, +18 does
    r = run(levels=[lv("s", 7783.0), lv("t", 7816.0), lv("t2", 7818.0, tf="4h")])
    assert r.stop_points == 18.0 and r.target_price == 7818.0 and r.rr == 1.0


def test_rr_two_breakeven_third():
    # base stop 15, target +30 -> rr 2 -> 33.33%
    r = run(levels=[lv("t", 7830.0)])
    assert r.rr == pytest.approx(2.0) and r.breakeven_win_rate == pytest.approx(100 / 3)
    assert r.target_points == 30.0


def test_rr_fractional():
    # stop 18 (anchored), target +30 -> rr 1.6667 -> be 37.5%
    r = run(levels=[lv("s", 7783.0), lv("t", 7830.0)])
    assert r.rr == pytest.approx(30 / 18) and r.breakeven_win_rate == pytest.approx(37.5)


def test_tick_rounding_long_stop_goes_down():
    # level 7784.90 d=15.1; buffer 1.0 -> raw 7783.90 -> floor to grid 7783.75 ; dist 16.25 (65 ticks)
    r = run(levels=[lv("x", 7784.90)])
    assert r.stop_price == 7783.75 and r.stop_points == 16.25 and r.stop_ticks == 65


def test_tick_rounding_short_stop_goes_up():
    # level 7815.10 d=15.1; raw 7816.10 -> ceil to grid 7816.25 ; dist 16.25
    r = run("SHORT", [lv("x", 7815.10)])
    assert r.stop_price == 7816.25 and r.stop_points == 16.25 and r.stop_ticks == 65


def test_base_rounds_up_to_tick():
    # atr 10.1: 1.5 x 10.1 = 15.15 -> /0.25 = 60.6 -> 61 ticks = 15.25 ; cap 20.2
    r = run(atr=10.1)
    assert r.stop_ticks == 61 and r.stop_points == 15.25 and r.stop_price == 7784.75


def test_buffer_two_ticks_floor_when_atr_small():
    # atr 4: D0=6.0 (24 ticks), cap 8.0, buffer=max(0.5, 0.4)=0.5. level d=7 -> stop dist 7.5
    r = run(atr=4.0, levels=[lv("x", 7793.0)])
    assert r.stop_points == 7.5 and r.anchored_to is not None


def test_target_rounds_to_nearest_tick():
    assert run(levels=[lv("t", 7830.10)]).target_price == 7830.0
    assert run(levels=[lv("t", 7830.13)]).target_price == 7830.25
    # rounding must not let a sub-1R level through: 7814.9 -> 7815.0 is exactly 1R and passes; 7814.8 -> 7814.75 fails
    assert run(levels=[lv("t", 7814.9)]).target_price == 7815.0
    assert run(levels=[lv("t", 7814.8)]).target_price is None


def test_duplicate_ids_ignored_not_crash():
    r = run(levels=[lv("d", 7783.0), lv("d", 7782.0)])   # second (same id+tf) dropped
    assert r.stop_points == 18.0
    # same id on a different timeframe is a distinct level
    r2 = run(levels=[lv("d", 7783.0), lv("d", 7784.0, tf="1h")])
    assert r2.anchored_to == {"id": "d", "tf": "1h"}


def test_wrong_side_and_zero_distance_levels_ignored():
    # for LONG: a level above entry is not adverse; level at entry has zero distance
    r = run(levels=[lv("up", 7790.0), lv("at", 7800.0)])
    assert r.anchored_to is None and r.stop_points == 15.0
    # and a "target" below entry for LONG is not favorable
    assert r.target_price is None


def test_hostile_levels_entries_ignored():
    bad = [None, 5, "x", [], {}, {"id": "a"}, {"price": 1.0}, lv("a", float("nan")), lv("b", float("inf")),
           lv("c", True), lv("d", "7783"), lv("e", -5.0), lv("f", 0.0), lv(7, 7783.0), lv("", 7783.0),
           {"id": "g", "verdict": "ABOVE_RANDOM"}]
    r = run(levels=bad)
    assert r.ok and r.stop_points == 15.0 and r.anchored_to is None and r.target_price is None


def test_levels_wrong_type_fails_closed():
    assert not A.anchor_stop_target("LONG", E, ATR, TS, TV, "abc").ok
    assert not A.anchor_stop_target("LONG", E, ATR, TS, TV, {"a": 1}).ok
    assert A.anchor_stop_target("LONG", E, ATR, TS, TV, None).ok
    assert A.anchor_stop_target("LONG", E, ATR, TS, TV, []).ok


@pytest.mark.parametrize("name,kw", [
    ("entry", {"entry": float("nan")}), ("entry", {"entry": float("inf")}), ("entry", {"entry": True}),
    ("entry", {"entry": "7800"}), ("entry", {"entry": -1.0}), ("entry", {"entry": 0}), ("entry", {"entry": None}),
    ("atr", {"atr": float("nan")}), ("atr", {"atr": -float("inf")}), ("atr", {"atr": True}), ("atr", {"atr": "10"}),
    ("atr", {"atr": 0}), ("atr", {"atr": -3.0}), ("atr", {"atr": None}),
    ("ts", {"ts": 0}), ("ts", {"ts": float("nan")}), ("ts", {"ts": True}), ("ts", {"ts": "0.25"}), ("ts", {"ts": -0.25}),
    ("tv", {"tv": 0}), ("tv", {"tv": float("inf")}), ("tv", {"tv": False}), ("tv", {"tv": None}), ("tv", {"tv": "1"}),
    ("k", {"k_base": 0}), ("k", {"k_base": float("nan")}), ("k", {"k_base": True}), ("k", {"k_cap": 1.0}),
    ("k", {"k_cap": float("inf")}), ("k", {"min_ticks": 0}), ("k", {"min_ticks": True}), ("k", {"min_ticks": 1.5}),
    ("entry", {"entry": 7800.1}),   # off the tick grid
])
def test_hostile_scalars_fail_closed(name, kw):
    r = run(**kw)
    assert r.ok is False and r.reasons and r.stop_price is None and r.target_price is None


@pytest.mark.parametrize("side", ["long", "BUY", "", None, 1, True, ["LONG"], "LONG "])
def test_bad_side(side):
    r = run(side)
    assert r.ok is False and r.reasons


def test_stop_would_cross_zero():
    r = run(entry=5.0, atr=10.0)
    assert not r.ok


def test_tiny_atr_tick_floor_exceeds_cap_fails_closed():
    # atr 0.2: D0 = max(ceil(0.3/0.25)=2, min_ticks 4) = 4 ticks = 1.0 > cap 0.4 -> refuse
    r = run(atr=0.2)
    assert not r.ok and "exceeds" in r.reasons[0]
    # with min_ticks=1: D0=2 ticks=0.5 > cap 0.4 still refuses
    assert not run(atr=0.2, min_ticks=1).ok


def test_matches_sizing_stop_from_atr():
    from projectx_desk import sizing
    for atr in (3.37, 8.0, 10.1, 12.55, 25.0):
        r = run(atr=atr)
        assert r.stop_ticks == sizing.stop_from_atr(atr, TS)


def test_to_dict_roundtrip_has_fields():
    d = run(levels=[lv("s", 7783.0), lv("t", 7830.0)]).to_dict()
    for k in ("ok", "stop_price", "stop_points", "stop_ticks", "anchored_to", "reasons", "target_level", "rr",
              "breakeven_win_rate", "k_eff", "target_price"):
        assert k in d


def test_invariant_random():
    rng = random.Random(1234)
    ticks = [(0.25, 0.5), (0.25, 1.25), (0.1, 1.0), (1.0, 0.5), (0.01, 1.0), (0.1, 1.0)]
    for _ in range(4000):
        ts, tv = rng.choice(ticks)
        atr = rng.uniform(2.0, 60.0)
        entry = round(rng.uniform(2000, 30000) / ts) * ts
        side = rng.choice(["LONG", "SHORT"])
        kb = rng.choice([1.0, 1.5, 1.5, 2.0])
        kc = kb + rng.choice([0.0, 0.5, 0.5, 1.0])
        levels = []
        for i in range(rng.randint(0, 8)):
            levels.append(lv(f"l{i}", entry + rng.uniform(-4, 4) * atr, tf=rng.choice(["5m", "15m", "1h"]),
                             verdict=rng.choice(["ABOVE_RANDOM", "ABOVE_RANDOM", "BELOW_RANDOM", "UNKNOWN"])))
        r = A.anchor_stop_target(side, entry, atr, ts, tv, levels, k_base=kb, k_cap=kc)
        if not r.ok:
            assert r.stop_price is None
            continue
        d0 = r.base_points
        assert d0 - 1e-9 <= r.stop_points <= kc * atr + 1e-9
        assert r.k_eff * atr == pytest.approx(r.stop_points)
        if side == "LONG":
            assert r.stop_price < entry
        else:
            assert r.stop_price > entry
        assert abs(abs(entry - r.stop_price) - r.stop_points) < 1e-6
        assert r.stop_points / ts == pytest.approx(r.stop_ticks)
        # stop on the tick grid
        assert abs(r.stop_price / ts - round(r.stop_price / ts)) < 1e-6
        if r.target_price is not None:
            fav = r.target_price - entry if side == "LONG" else entry - r.target_price
            assert fav >= r.stop_points - 1e-6
            assert r.rr == pytest.approx(fav / r.stop_points)
            assert r.breakeven_win_rate == pytest.approx(100 / (1 + r.rr))
        else:
            assert r.rr is None and r.breakeven_win_rate is None


def test_never_raises_on_garbage():
    junk = [None, float("nan"), "x", [], {}, True, -1, 1e308, object()]
    for a in junk:
        for b in junk:
            for s in ("LONG", "SHORT", a):
                r = A.anchor_stop_target(s, a, b, a, b, [a, b], k_base=a, k_cap=b)
                assert isinstance(r, A.AnchorResult)


# --------------------------------------------------------- projection / heat


def test_projection_basic_and_floor():
    assert A.projection(1500.0, 800.0, 100.0) == {"drawdown_left": 1400.0, "daily_left": 700.0}
    assert A.projection(50.0, 80.0, 100.0) == {"drawdown_left": 0.0, "daily_left": 0.0}
    assert A.projection(100.0, 100.0, 100.0) == {"drawdown_left": 0.0, "daily_left": 0.0}


def test_projection_unreadable():
    r = A.projection(None, 800.0, 100.0)
    assert r == {"drawdown_left": None, "daily_left": 700.0}
    assert A.projection(float("nan"), "x", 100.0) == {"drawdown_left": None, "daily_left": None}
    assert A.projection(True, 5.0, 1.0)["drawdown_left"] is None
    for bad in (None, float("nan"), float("inf"), True, "5", -1.0):
        assert A.projection(1000.0, 1000.0, bad) == {"drawdown_left": None, "daily_left": None}
    assert A.projection(1000.0, 1000.0, 0) == {"drawdown_left": 1000.0, "daily_left": 1000.0}


def test_heat_basic():
    assert A.heat(100.0, 50.0, 1000.0) == {"total_usd": 150.0, "pct": 15.0}
    assert A.heat(0, 100, 2000) == {"total_usd": 100.0, "pct": 5.0}


def test_heat_none_safe():
    assert A.heat(None, 50.0, 1000.0) == {"total_usd": None, "pct": None}
    assert A.heat(10.0, None, 1000.0) == {"total_usd": None, "pct": None}
    assert A.heat(10.0, 5.0, None) == {"total_usd": 15.0, "pct": None}
    assert A.heat(10.0, 5.0, 0) == {"total_usd": 15.0, "pct": None}
    assert A.heat(10.0, 5.0, -3.0)["pct"] is None
    assert A.heat(10.0, 5.0, float("nan"))["pct"] is None
    assert A.heat(True, 5.0, 100.0)["total_usd"] is None
    assert A.heat(-1.0, 5.0, 100.0) == {"total_usd": None, "pct": None}
    assert A.heat("a", "b", "c") == {"total_usd": None, "pct": None}
