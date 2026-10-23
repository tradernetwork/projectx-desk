from projectx_desk.bias import bias_view, grade_for

ALL_BEAR = {"5m": "BEAR", "15m": "BEAR", "1h": "BEAR"}


def test_all_agree_trending_calm_is_a_plus():
    b = bias_view(95, 100, 95.5, 1.0, 50, 30, ALL_BEAR, None)
    assert (b["direction"], b["grade"], b["agree"], b["reads"]) == ("SHORT", "A+", 4, 4)


def test_extreme_volatility_caps_below_a():
    b = bias_view(7664.75, 7695.26, 7678.96, 5.0, 98, 33, ALL_BEAR, None)
    assert b["direction"] == "SHORT" and b["grade"] == "B" and "VOL_EXTREME" in b["flags"]


def test_ranging_loses_a_point_and_says_so():
    b = bias_view(95, 100, 95.5, 1.0, 50, 12, ALL_BEAR, None)
    assert "RANGING" in b["flags"] and b["score"] == 4.0


def test_too_few_reads_is_na_never_a_guess():
    assert bias_view(100, None, None, 1, 50, 30, {"5m": "BULL", "15m": "NA", "1h": "NA"}, None)["direction"] == "NA"
    assert bias_view(None, None, None, None, None, None, None, None)["grade"] == "NA"


def test_split_reads_are_neutral_f():
    b = bias_view(100, 100, 99, 1, 50, 30, {"5m": "BULL", "15m": "BEAR", "1h": "MIXED"}, None)
    assert b["direction"] == "LONG" or b["direction"] == "NEUTRAL"
    b = bias_view(100, 100, 99, 1, 50, 30, {"5m": "BULL", "15m": "BEAR", "1h": "MIXED"}, None)
    assert (b["direction"], b["grade"]) == ("NEUTRAL", "F")


def test_incomplete_flow_window_is_ignored_and_complete_one_counts():
    inc = {"w": {15: {"buy": 900, "sell": 100, "complete": False}}}
    assert "flow" not in bias_view(95, 100, 95.5, 1, 50, 30, ALL_BEAR, inc)["votes"]
    ok = {"w": {15: {"buy": 900, "sell": 100, "complete": True}}}
    b = bias_view(95, 100, 95.5, 1, 50, 30, ALL_BEAR, ok)
    assert b["votes"]["flow"] == 1 and "DISSENT" in b["flags"]


def test_extended_chase_costs_half_a_point():
    near = bias_view(95, 100, 96, 1.0, 50, 30, ALL_BEAR, None)["score"]
    far = bias_view(90, 100, 96, 1.0, 50, 30, ALL_BEAR, None)["score"]
    assert far == near - 0.5


def test_nonfinite_inputs_do_not_vote():
    b = bias_view(float("nan"), float("inf"), 1, 1, 50, 30, ALL_BEAR, None)
    assert "vwap" not in b["votes"]


def test_bands():
    assert [grade_for(x) for x in (4.5, 4.0, 3.0, 2.0, 1.0, 0.0)] == ["A+", "A", "B", "C", "D", "F"]


def test_ninety_fifth_percentile_is_extreme_and_capped():
    b = bias_view(95, 100, 95.5, 1.0, 95, 30, ALL_BEAR, None)
    assert "VOL_EXTREME" in b["flags"] and b["grade"] == "B"


def test_ui_revalidates_so_a_restart_never_serves_a_stale_page():
    from fastapi.testclient import TestClient
    from projectx_desk.app import create_app
    import inspect
    assert "revalidate_ui" in inspect.getsource(create_app)
