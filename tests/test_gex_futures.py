"""Tests for projectx.gex_futures (pure math, hermetic)."""

import copy
import importlib.util
import math
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "projectx_desk" / "gex_futures.py"
_spec = importlib.util.spec_from_file_location("projectx_gex_futures", _SRC)
gf = importlib.util.module_from_spec(_spec)
sys.modules["projectx_gex_futures"] = gf
_spec.loader.exec_module(gf)


def spx_levels(**over):
    d = {
        "symbol": "SPX", "spot": 7422.13, "flip": 7430.40, "flip_gex": 7424.45,
        "flip_apex": 7430.40, "flip_split": False, "flip_source": "apex",
        "pin": 7680.0, "regime": "positive gamma", "net_gex": 22.49e9,
        "walls": [{"strike": 7400.0, "net_gex": -1e9, "side": "below"},
                  {"strike": 7680.0, "net_gex": 3e9, "side": "above"}],
        "apex_note": "apex unavailable (gated)",
    }
    d.update(over)
    return d


def spy_levels(**over):
    d = {"symbol": "SPY", "spot": 742.0, "flip": None, "pin": 745.0,
         "flip_split": False, "walls": [{"strike": 750.0, "net_gex": 1e9, "side": "above"}]}
    d.update(over)
    return d


def conv_spx(levels=None, last=7452.25, spot=7422.13, tick=0.25, age=10, **kw):
    return gf.convert_levels(levels or spx_levels(), "ES", last, spot, tick, age, **kw)


def test_additive_cash_index_hand_checked():
    r = conv_spx()
    assert r.ok and r.method == "additive" and r.source_symbol == "SPX"
    assert r.basis == pytest.approx(30.12)
    assert r.ratio is None and r.approximate is False
    # 7430.40 + 30.12 = 7460.52 -> 7460.50 ; 7680 + 30.12 = 7710.12 -> 7710.00
    assert r.levels["flip"] == 7460.50
    assert r.levels["pin"] == 7710.00
    assert r.levels["flip_gex"] == 7454.50   # 7424.45 + 30.12 = 7454.57
    assert r.levels["walls"][0]["strike"] == 7430.00  # 7430.12
    assert r.levels["spot"] == 7452.25


def test_ratio_etf_hand_checked():
    # ratio = 7430 / 742 = 10.013477...
    r = gf.convert_levels(spy_levels(), "ES", 7430.0, 742.0, 0.25, 5)
    assert r.ok and r.method == "ratio" and r.source_symbol == "SPY"
    assert r.ratio == pytest.approx(7430 / 742, rel=1e-6)
    assert r.basis == 0.0
    assert r.levels["pin"] == 7460.00                 # 7460.04
    assert r.levels["walls"][0]["strike"] == 7510.00  # 7510.108
    assert r.levels["spot"] == 7430.00


def test_additive_and_ratio_differ_on_same_inputs():
    # Same underlying/futures pair, forced method difference via source kind.
    add = gf.convert_levels(spx_levels(), "ES", 7452.25, 7422.13, 0.25, 1)
    rat = gf.convert_levels(spx_levels(symbol="SPY"), "ES", 7452.25, 7422.13 / 10, 0.25, 1)
    assert add.method == "additive" and rat.method == "ratio"
    assert add.levels["pin"] != rat.levels["pin"]


def test_tick_rounding_half_up_and_fine_ticks():
    lv = {"symbol": "SPX", "flip": 5000.0, "pin": None}
    r = gf.convert_levels(lv, "ES", 5000.13, 5000.0, 0.25, 1)
    assert r.levels["flip"] == 5000.25   # 5000.13 / .25 = 20000.52
    r2 = gf.convert_levels(lv, "ES", 5000.10, 5000.0, 0.25, 1)
    assert r2.levels["flip"] == 5000.00  # 20000.4
    r3 = gf.convert_levels({"symbol": "GLD", "flip": 400.0}, "GC", 4320.0, 400.0, 0.1, 1)
    assert r3.levels["flip"] == 4320.0
    assert (r3.levels["flip"] / 0.1) == pytest.approx(round(r3.levels["flip"] / 0.1))


def test_none_levels_pass_through():
    r = gf.convert_levels(spy_levels(), "ES", 7430.0, 742.0, 0.25, 1)
    assert r.levels["flip"] is None
    r2 = gf.convert_levels(spx_levels(flip=None, flip_apex=None), "ES", 7452.25, 7422.13, 0.25, 1)
    assert r2.ok and r2.levels["flip"] is None and r2.levels["flip_apex"] is None


def test_flip_split_and_flags_preserved_untouched():
    lv = spx_levels(flip_split=True, flip_gex=7415.0, flip_apex=7430.0, flip=7430.0)
    before = copy.deepcopy(lv)
    r = conv_spx(lv)
    assert lv == before                      # input not mutated
    assert r.levels["flip_split"] is True
    assert r.levels["flip_source"] == "apex"
    assert r.levels["apex_note"] == "apex unavailable (gated)"
    assert r.levels["regime"] == "positive gamma"
    assert r.levels["net_gex"] == 22.49e9
    # both flips converted independently; none picked or merged
    assert r.levels["flip_gex"] != r.levels["flip_apex"]
    assert r.levels["walls"][1]["side"] == "above"


def test_stale_refused_and_no_levels_returned():
    r = conv_spx(age=301)
    assert not r.ok and "stale" in r.reason and r.levels is None
    assert conv_spx(age=300).ok                      # boundary is inclusive
    assert not conv_spx(age=10, max_age_s=5).ok
    assert conv_spx(age=500, max_age_s=600).ok


def test_missing_or_bad_age_fails_closed():
    for bad in (None, -1, float("nan"), "5", True):
        r = gf.convert_levels(spx_levels(), "ES", 7452.25, 7422.13, 0.25, bad)
        assert not r.ok and r.levels is None


def test_implausible_basis_refused():
    r = conv_spx(last=7422.13 * 1.05)
    assert not r.ok and "basis" in r.reason and r.levels is None
    assert conv_spx(last=7422.13 * 1.019).ok
    assert conv_spx(last=7422.13 * 1.05, max_basis_pct=0.10).ok


def test_implausible_etf_ratio_refused():
    # Futures in index points vs an ETF quote that is clearly the wrong thing.
    r = gf.convert_levels(spy_levels(), "ES", 7430.0, 7420.0, 0.25, 1)  # ratio ~1.0, nominal 10
    assert not r.ok and "ratio" in r.reason


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), None, "7400", True])
def test_bad_prices_refused(bad):
    assert not gf.convert_levels(spx_levels(), "ES", bad, 7422.13, 0.25, 1).ok
    assert not gf.convert_levels(spx_levels(), "ES", 7452.25, bad, 0.25, 1).ok
    assert not gf.convert_levels(spx_levels(), "ES", 7452.25, 7422.13, bad, 1).ok


def test_bad_level_value_and_bad_inputs_refused():
    assert not conv_spx(spx_levels(pin=float("nan"))).ok
    assert not conv_spx(spx_levels(pin=-3.0)).ok
    assert not gf.convert_levels({}, "ES", 7452.25, 7422.13, 0.25, 1).ok
    assert not gf.convert_levels(None, "ES", 7452.25, 7422.13, 0.25, 1).ok
    assert not gf.convert_levels({"symbol": "SPX", "error": "TD_API_KEY not set"}, "ES", 7452.25, 7422.13, 0.25, 1).ok
    assert not gf.convert_levels(spx_levels(), "ZZ", 7452.25, 7422.13, 0.25, 1).ok


def test_source_must_match_future():
    r = gf.convert_levels(spx_levels(symbol="QQQ"), "ES", 7452.25, 7422.13, 0.25, 1)
    assert not r.ok and "not a valid underlying" in r.reason


def test_micros_alias_and_mapping_table():
    r = gf.convert_levels(spx_levels(), "MES", 7452.25, 7422.13, 0.25, 1)
    assert r.ok and r.futures_symbol == "MES"
    for micro, root in gf.ALIASES.items():
        assert root in gf.MAPPING
    assert gf.MAPPING["ES"]["sources"][0]["kind"] == gf.CASH_INDEX
    assert all(s["kind"] == gf.ETF for s in gf.MAPPING["GC"]["sources"])


def test_gc_flagged_approximate_and_cl_unsupported():
    r = gf.convert_levels({"symbol": "GLD", "flip": 400.0}, "MGC", 4320.0, 400.0, 0.1, 1)
    assert r.ok and r.approximate and r.notes
    r2 = gf.convert_levels({"symbol": "USO", "flip": 70.0}, "CL", 70.0, 70.0, 0.01, 1)
    assert not r2.ok and "unsupported" in r2.reason
    r3 = gf.convert_levels({"symbol": "USO", "flip": 70.0}, "CL", 70.0, 70.0, 0.01, 1, allow_unsupported=True)
    assert r3.ok and r3.approximate


def test_panel_ordering_distances_and_disclaimer():
    r = conv_spx()
    p = gf.describe_for_panel(r, 7452.25)
    assert p["disclaimer"] == gf.DISCLAIMER == "GEX marks positioning, not targets; the flip is a regime boundary, not a forecast."
    assert p["status"] == "ok"
    assert p["pin"] == "7710.00 | +257.75 pts | +1031 ticks | above last"
    assert "below last" in p["walls_1"]
    # high to low
    keys = [k for k in p if k in ("pin", "flip", "flip_gex", "walls_1", "walls_2")]
    prices = [float(p[k].split()[0]) for k in keys]
    assert prices == sorted(prices, reverse=True)


def test_panel_shows_both_flips_on_split_and_never_picks():
    lv = spx_levels(flip_split=True, flip_gex=7415.0, flip_apex=7430.0, flip=7430.0)
    p = gf.describe_for_panel(conv_spx(lv), 7452.25)
    assert "flip_split" in p and "none chosen" in p["flip_split"]
    assert "flip_gex" in p and "flip_apex" in p


def test_panel_refusal_and_bad_last():
    p = gf.describe_for_panel(conv_spx(age=999), 7452.25)
    assert p["status"].startswith("NO LEVELS") and "flip" not in p
    assert p["disclaimer"] == gf.DISCLAIMER
    assert gf.describe_for_panel(conv_spx(), float("nan"))["status"].startswith("NO LEVELS")


def test_no_em_dash_in_module():
    assert chr(0x2014) not in _SRC.read_text(encoding="utf-8")


def test_unrecognised_strike_list_entry_refuses_instead_of_passing_through():
    r = gf.convert_levels({"spot": 6000.0, "walls": [6100.0]}, "ES", futures_last=6012.0,
                         underlying_spot=6000.0, tick_size=0.25, asof_age_s=10, source_symbol="SPX")
    assert r.ok is False and "unrecognised" in r.reason


def test_walls_dict_strikes_are_shifted_by_basis():
    r = gf.convert_levels({"spot": 6000.0, "walls": [{"strike": 6100.0, "netGex": 5}]}, "ES",
                         futures_last=6012.0, underlying_spot=6000.0, tick_size=0.25, asof_age_s=10,
                         source_symbol="SPX")
    assert r.ok and r.levels["walls"][0]["strike"] == 6112.0 and r.levels["walls"][0]["netGex"] == 5
