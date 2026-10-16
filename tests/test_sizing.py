"""Tests for projectx.sizing (pure math, hermetic)."""

import importlib.util
import math
import random
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "sizing.py"
_spec = importlib.util.spec_from_file_location("projectx_sizing", _SRC)
sz = importlib.util.module_from_spec(_spec)
sys.modules["projectx_sizing"] = sz
_spec.loader.exec_module(sz)

SizingError = sz.SizingError


def test_floor_never_rounds_up():
    # $99 / (40 ticks * $1.25 = $50) = 1.98 -> 1
    r = sz.size_position(99, 40, 1.25)
    assert r.contracts == 1 and r.actual_risk == 50.0
    assert r.unused_risk == pytest.approx(49.0)
    assert r.binding == "risk" and not r.refused


def test_exact_fit():
    r = sz.size_position(50, 40, 1.25)
    assert r.contracts == 1 and r.unused_risk == 0.0


def test_decimal_edge_does_not_lose_a_contract():
    # 0.3 / 0.1 is 2.9999999999999996 in float math
    r = sz.size_position(0.3, 1, 0.1)
    assert r.contracts == 3


def test_risk_never_exceeded_property():
    rng = random.Random(1234)
    for _ in range(5000):
        risk = rng.uniform(0.01, 10_000)
        ticks = rng.choice([1, 2, 3, 4, 7, 16, 40, 100, rng.randint(1, 500)])
        tv = rng.choice([0.5, 1.0, 1.25, 5.0, 10.0, 12.5])
        cap = rng.choice([None, 1, 3, 10])
        r = sz.size_position(risk, ticks, tv, cap)
        assert r.actual_risk <= risk
        assert r.unused_risk >= 0
        assert r.contracts >= 0
        if cap is not None:
            assert r.contracts <= cap
        # one more contract would break the budget (unless capped)
        if r.binding == "risk":
            assert (r.contracts + 1) * r.risk_per_contract > risk


def test_granularity_refusal():
    r = sz.size_position(20, 40, 1.25)
    assert r.contracts == 0 and r.refused and r.binding == "granularity"
    assert r.actual_risk == 0.0 and r.unused_risk == 20.0
    assert "50.00" in r.reason


def test_max_contracts_binds():
    r = sz.size_position(1000, 10, 1.25, max_contracts=2)
    assert r.contracts == 2 and r.binding == "max_contracts"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), None, "5", True])
def test_bad_inputs_fail_closed(bad):
    with pytest.raises(SizingError):
        sz.size_position(bad, 10, 1.25)
    with pytest.raises(SizingError):
        sz.size_position(100, bad, 1.25)
    with pytest.raises(SizingError):
        sz.size_position(100, 10, bad)


@pytest.mark.parametrize("cap", [0, -2, 1.5, True])
def test_bad_max_contracts(cap):
    with pytest.raises(SizingError):
        sz.size_position(100, 10, 1.25, max_contracts=cap)


def test_atr_hand_checked():
    h = [10, 11, 12, 11, 13]
    l = [8, 9, 10, 9, 10]
    c = [9, 10, 11, 10, 12]
    # TR = 2, 2, 2, 3 ; seed mean(2,2,2)=2 ; (2*2+3)/3
    assert sz.atr(h, l, c, n=3) == pytest.approx(7 / 3)


def test_atr_not_enough_bars():
    assert sz.atr([1, 2, 3], [0, 1, 2], [1, 2, 3], n=3) is None


def test_adx_hand_checked():
    h = [10, 11, 12, 11, 13]
    l = [8, 9, 10, 9, 10]
    c = [9, 10, 11, 10, 12]
    # n=2. +DM = 1,1,0,2 ; -DM = 0,0,1,0 ; TR = 2,2,2,3
    # seed sums TR4 +DM2 -DM0 -> DX 100
    # step3: TR4 +DM1 -DM1 -> DX 0 ; ADX seed = 50
    # step4: TR5 +DM2.5 -DM0.5 -> +DI 50, -DI 10, DX 66.667
    # ADX = (50 + 66.667) / 2 = 58.333
    a, pdi, mdi = sz.adx(h, l, c, n=2)
    assert pdi == pytest.approx(50.0)
    assert mdi == pytest.approx(10.0)
    assert a == pytest.approx(175 / 3)


def test_adx_not_enough_bars():
    assert sz.adx([1, 2, 3, 4], [0, 1, 2, 3], [1, 2, 3, 4], n=2) is None


def test_indicator_rejects_nan_and_length_mismatch():
    with pytest.raises(SizingError):
        sz.atr([1, float("nan"), 3], [0, 1, 2], [1, 2, 3], n=1)
    with pytest.raises(SizingError):
        sz.atr([1, 2], [0, 1, 2], [1, 2, 3], n=1)


def test_stop_from_atr_rounds_up():
    # 1.5 * 2.1 / 0.25 = 12.6 -> 13
    assert sz.stop_from_atr(2.1, 0.25) == 13
    # exactly 12.0 stays 12 (no float fuzz bump)
    assert sz.stop_from_atr(2.0, 0.25) == 12
    # floor at min_ticks
    assert sz.stop_from_atr(0.1, 0.25) == 4
    with pytest.raises(SizingError):
        sz.stop_from_atr(0, 0.25)


def test_risk_budget_min_of_limits():
    assert sz.risk_budget(50_000, 0.01) == 500.0
    assert sz.risk_budget(50_000, 0.01, daily_loss_remaining=300) == 300.0
    assert sz.risk_budget(50_000, 0.01, 900, 200) == 200.0


def test_risk_budget_fails_closed():
    assert sz.risk_budget(50_000, 0.01, daily_loss_remaining=0) == 0.0
    assert sz.risk_budget(50_000, 0.01, drawdown_buffer_remaining=-5) == 0.0
    assert sz.risk_budget(-1, 0.01) == 0.0
    with pytest.raises(SizingError):
        sz.risk_budget(50_000, 0)
    with pytest.raises(SizingError):
        sz.risk_budget(float("nan"), 0.01)


def test_spec_parsing_and_values():
    s = sz.spec_for("CON.F.US.MES.Z25")
    assert (s.root, s.tick_size, s.tick_value, s.point_value) == ("MES", 0.25, 1.25, 5.0)
    assert s.micro_of == "ES" and not s.live
    assert sz.spec_for("es").full_size_of == "MES"
    for root, (ts, tv, pv, _rel) in sz.CONTRACT_SPECS.items():
        assert tv / ts == pytest.approx(pv), root


def test_unknown_root_raises():
    with pytest.raises(SizingError):
        sz.spec_for("CON.F.US.ZZZ.Z25")
    with pytest.raises(SizingError):
        sz.spec_for("")
    with pytest.raises(SizingError):
        sz.spec_for("A.B")


def test_live_tick_value_overrides():
    s = sz.spec_for("MES", live_tick_size=0.25, live_tick_value=1.30)
    assert s.tick_value == 1.30 and s.live
    assert s.point_value == pytest.approx(5.2)
    for bad in (0, -1, float("nan")):
        with pytest.raises(SizingError):
            sz.spec_for("MES", live_tick_value=bad)
        with pytest.raises(SizingError):
            sz.spec_for("MES", live_tick_size=bad)


def test_choose_contract_micro_when_full_does_not_fit():
    # 10 pt stop: MES = 40 ticks * 1.25 = $50 ; ES = 40 * 12.5 = $500
    c = sz.choose_contract("ES", 120, 10, adx_value=31.5)
    assert c.recommended == "MES" and not c.refused
    assert c.options["ES"].refused
    assert c.options["MES"].contracts == 2
    assert c.options["MES"].unused_risk == pytest.approx(20.0)
    assert c.adx_value == 31.5


def test_choose_contract_prefers_full_when_it_wastes_less():
    # $520 risk, 10 pt: ES 1 contract ($500, waste 20) vs MES 10 ($500, waste 20): tie -> micro
    c = sz.choose_contract("MES", 520, 10)
    assert c.recommended == "MES"
    # NQ 5 pt = 20 ticks: NQ $100/ct, MNQ $10/ct. $155: NQ 1 (waste 55), MNQ 15 (waste 5) -> micro
    assert sz.choose_contract("NQ", 155, 5).recommended == "MNQ"
    # YM: 10 pt = 10 ticks. YM $50/ct, MYM $5/ct. Both divide cleanly -> tie -> micro
    assert sz.choose_contract("YM", 100, 10).recommended == "MYM"


def test_choose_contract_full_wins_with_custom_specs():
    specs = {
        "MX": (1.0, 3.0, 3.0, ("micro_of", "XX")),
        "XX": (1.0, 7.0, 7.0, ("full_size_of", "MX")),
    }
    # 10 ticks: MX $30/ct, XX $70/ct. $70 risk: MX 2 (waste 10), XX 1 (waste 0)
    c = sz.choose_contract("MX", 70, 10, specs=specs)
    assert c.recommended == "XX"
    # raising the full_min_contracts threshold flips it back
    c2 = sz.choose_contract("MX", 70, 10, specs=specs, full_min_contracts=2)
    assert c2.recommended == "MX"


def test_choose_contract_refuses_when_neither_fits():
    c = sz.choose_contract("MES", 10, 10)  # MES needs $50
    assert c.refused and c.recommended is None


def test_choose_contract_unknown_root():
    with pytest.raises(SizingError):
        sz.choose_contract("ZZZ", 100, 10)


def test_r_multiple_expectancy_break_even():
    assert sz.r_multiple(100, 103, 98, "long") == pytest.approx(1.5)
    assert sz.r_multiple(100, 97, 102, "short") == pytest.approx(1.5)
    assert sz.r_multiple(100, 98, 98, "buy") == pytest.approx(-1.0)
    with pytest.raises(SizingError):
        sz.r_multiple(100, 103, 101, "long")
    with pytest.raises(SizingError):
        sz.r_multiple(100, 103, 98, "flat")
    assert sz.expectancy([2, -1, -1, 3]) == pytest.approx(0.75)
    assert sz.expectancy([]) is None
    assert sz.break_even_win_rate(2.0, 1.0) == pytest.approx(1 / 3)
    assert sz.break_even_win_rate(1.0, -1.0) == pytest.approx(0.5)
    with pytest.raises(SizingError):
        sz.break_even_win_rate(0, 1)


def test_describe_trade_fields_and_order():
    d = sz.describe_trade("CON.F.US.MES.Z25", "long", 5000.0, 4990.0, 100, risk_budget_dollars=200)
    assert list(d)[:3] == ["contract", "side", "entry"]
    assert d["contract"] == "MES"
    assert d["side"] == "BUY (long)"
    assert d["stop_ticks"] == 40
    assert d["risk_per_contract"] == 50.0
    assert d["contracts"] == 2
    assert d["total_risk_dollars"] == 100.0
    assert d["pct_of_risk_budget_used"] == pytest.approx(50.0)
    assert d["reward_1r_dollars"] == 100.0
    assert d["reward_2r_dollars"] == 200.0
    assert d["reward_3r_dollars"] == 300.0
    assert d["binding_constraint"] == "risk"
    assert d["refused"] is False


def test_describe_trade_refused_and_validation():
    d = sz.describe_trade("MES", "short", 5000.0, 5010.0, 20)
    assert d["contracts"] == 0 and d["refused"] and d["binding_constraint"] == "granularity"
    assert d["side"] == "SELL (short)"
    with pytest.raises(SizingError):
        sz.describe_trade("MES", "long", 5000.0, 5010.0, 100)
    with pytest.raises(SizingError):
        sz.describe_trade("ZZZ", "long", 5000.0, 4990.0, 100)


def test_describe_trade_uses_live_tick_value():
    d = sz.describe_trade("MES", "long", 5000.0, 4990.0, 100, live_tick_value=2.0)
    assert d["tick_value"] == 2.0 and d["risk_per_contract"] == 80.0 and d["contracts"] == 1
    assert math.isfinite(d["pct_of_risk_budget_used"])


def test_a_stop_must_be_a_whole_number_of_ticks_and_absurd_magnitudes_are_sizing_errors_not_crashes():
    import pytest
    with pytest.raises(sz.SizingError):
        sz.size_position(100, 2.5, 1.25)                     # a stop cannot sit between ticks
    for bad in (1e300, 10 ** 400, float("inf"), float("nan"), True, "5"):
        with pytest.raises(sz.SizingError):
            sz.size_position(bad, 3, 2)
        with pytest.raises(sz.SizingError):
            sz.size_position(3, bad, 2)
    with pytest.raises(sz.SizingError):
        sz.stop_from_atr(3, 10 ** 400)
    with pytest.raises(sz.SizingError):
        sz.risk_scale(3, float("inf"))


def test_describe_trade_never_sizes_beyond_the_remaining_budget():
    d = sz.describe_trade("MES", "long", 5000.0, 4990.0, 500.0, risk_budget_dollars=100.0)
    assert d["total_risk_dollars"] <= 100.0
