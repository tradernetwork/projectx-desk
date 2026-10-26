"""Tomorrow's Map. Parity with the TraderMatrix builder (itself pinned to the mjs) on three captured SPY books, strict equality on the
exact sentences, plus the rules that exist because the tool was once confidently wrong."""
import json
import pathlib

import pytest

from projectx_desk import tomorrow as T

FX = pathlib.Path(__file__).resolve().parent / "fixtures" / "tomorrow"
NAMES = ["spy-2026-10-17", "spy-2026-10-17-brake-ceiling", "spy-2026-10-17-shelf-cushion"]


def load(name):
    return json.loads((FX / f"{name}.json").read_text(encoding="utf-8"))


def build(fx):
    ladder, share = T.survivor_ladder(fx["matrix"], fx["today"])
    g = fx["gex"]
    return T.build_tomorrow_map(g["spotPrice"], g["gammaFlipLevel"], g["totalGEX"], ladder, T.bars_from_history(fx["historical"]), share)


def wp(m, road, suffix):
    r = next(x for x in m["roads"] if x["key"] == road)
    return next((w["price"] for w in r["waypoints"] if w["tag"].endswith(suffix)), None)


@pytest.mark.parametrize("name", NAMES)
def test_parity_with_the_typescript_builder(name):
    fx = load(name)
    e, m = fx["expected"], build(fx)
    assert m is not None
    assert m["typical_move"] == pytest.approx(e["expected"], abs=0.006)
    assert (m["flip"], m["pin"], m["thin_book"], m["expiring_share"]) == (e["flip"], e["pin"], e["thinBook"], e["expiringShare"])
    lv = e["levels"]
    assert wp(m, "UP", "ACCELERANT") == lv["accel"] and wp(m, "UP", "BRAKE") == lv["brake"] and wp(m, "UP", "CEILING") == lv["ceiling"]
    down = next(x for x in m["roads"] if x["key"] == "DOWN")["waypoints"]
    shelf = next((w["price"] for w in down if w["tag"].endswith(("TRAPDOOR", "LAST SHELF"))), None)
    assert shelf == lv["shelf"] and wp(m, "DOWN", "THIN CUSHION") == lv["cushion"] and wp(m, "DOWN", "PUT WALL") == lv["putWall"]
    for (kind, cond, rule), want in zip(T.branches(m), e["branches"]):
        assert (kind, cond, rule) == (want["kind"], want["cond"], want["rule"])


def test_the_headline_numbers_on_the_captured_book():
    m = build(load("spy-2026-10-17"))
    assert (m["typical_move"], m["flip"], m["regime"]) == (11.01, 776.41, "negative")
    assert [r["cond"] for r in m["roads"]] == ["up through 767", "below 763", "between 763 and 767"]


def test_survivor_ladder_drops_expiring_gamma_and_reports_its_share():
    matrix = {"expirations": ["2026-10-16", "2026-10-19"], "rows": [{"strike": 100, "gex": [-300.0, 100.0]}, {"strike": 101, "gex": [None, 50.0]},
                                                                      {"strike": 102, "gex": [20.0, None]}]}
    ladder, share = T.survivor_ladder(matrix, "2026-10-16")           # the 10-16 expiry dies tonight
    assert ladder == [{"strike": 100, "netGex": 100.0}, {"strike": 101, "netGex": 50.0}]
    assert share == pytest.approx(1 - 150 / 470)                      # magnitudes per expiry, not |sum|


def test_no_measured_net_gex_means_no_map_not_a_calm_default():
    fx = load("spy-2026-10-17")
    ladder, share = T.survivor_ladder(fx["matrix"], fx["today"])
    bars = T.bars_from_history(fx["historical"])
    assert T.build_tomorrow_map(763.23, 776.41, None, ladder, bars, share) is None
    assert T.build_tomorrow_map(0, 776.41, -1e9, ladder, bars, share) is None
    assert T.build_tomorrow_map(763.23, None, -1e9, ladder[:2], bars, share) is None          # fewer than 3 strikes


def test_a_thin_book_says_so_and_asserts_no_structure():
    ladder = [{"strike": k, "netGex": 1000.0 * (1 if k % 2 else -1)} for k in range(95, 106)]
    m = T.build_tomorrow_map(100.0, None, 1.0, ladder, [], None)
    assert m["thin_book"] is True
    up, down = m["roads"][0], m["roads"][1]
    assert up["rule"] == "book too thin to map the upside." and up["waypoints"] == []
    assert down["rule"] == "book too thin to map the downside." and down["waypoints"] == []


def mk(spot, gex_by_strike, flip=None, net=-5e9):
    ladder = [{"strike": float(k), "netGex": float(v)} for k, v in sorted(gex_by_strike.items())]
    bars = [{"t": f"2026-10-{10 + i:02d}", "h": spot + 6, "l": spot - 6, "c": spot} for i in range(5)]
    return T.build_tomorrow_map(spot, flip, net, ladder, bars, 0.0)


def test_a_short_gamma_strike_above_spot_is_an_accelerant_and_a_big_negative_below_is_a_crowd_not_a_floor():
    book = {k: 0 for k in range(94, 107)}
    book.update({103: -9e8, 97: -9e8, 100: 3e8})
    m = mk(100.0, book)
    up, down = m["roads"][0], m["roads"][1]
    assert [w["tag"] for w in up["waypoints"]] == ["103 ACCELERANT"]
    assert "97 PUT WALL" in [w["tag"] for w in down["waypoints"]] and "a crowd, not a floor" in down["waypoints"][-1]["note"]


def test_the_empty_band_never_runs_through_a_real_strike_and_the_pin_is_never_air():
    book = {k: 0 for k in range(92, 108)}
    book.update({99: 8e8, 94: -9e8, 96: 5e7})                           # a shelf, a put wall, and one real strike between them
    m = mk(100.0, book)
    down = next(r for r in m["roads"] if r["key"] == "DOWN")
    tags = [w["tag"] for w in down["waypoints"]]
    assert "99 LAST SHELF" in tags and "94 PUT WALL" in tags
    assert "EMPTY" not in tags                                          # 96 carries gamma: that is a thin cushion, not air
    assert "96 THIN CUSHION" in tags


def test_the_three_roads_are_exhaustive_and_the_else_band_contains_spot():
    for name in NAMES:
        m = build(load(name))
        band = next(r for r in m["roads"] if r["key"] == "RANGE")["band"]
        assert band["lo"] <= m["spot"] <= band["hi"]
        assert [r["key"] for r in m["roads"]] == ["UP", "DOWN", "RANGE"]


def test_regime_includes_expiring_label_fires_above_ten_percent_as_printed():
    fx = load("spy-2026-10-17")
    ladder, _ = T.survivor_ladder(fx["matrix"], fx["today"])
    bars = T.bars_from_history(fx["historical"])
    g = fx["gex"]
    args = (g["spotPrice"], g["gammaFlipLevel"], g["totalGEX"], ladder, bars)
    assert T.build_tomorrow_map(*args, 0.104)["regime_includes_expiring"] is False          # prints as 10%: not above the line
    assert T.build_tomorrow_map(*args, 0.105)["regime_includes_expiring"] is True           # prints as 11%
    assert T.build_tomorrow_map(*args, None, regime_counts_unlisted_expiry=True)["regime_includes_expiring"] is True


def test_px_prints_strikes_bare_and_everything_else_to_two_places():
    assert [T._px(v) for v in (765.0, 7.5, 766.67, 762.5, 0.25)] == ["765", "7.5", "766.67", "762.5", "0.25"]
