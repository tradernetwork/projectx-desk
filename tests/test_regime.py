"""Chop measures: the arithmetic, the JS twin used by the mini charts, and the fields that reach the bias log."""
import json
import math
import random
import shutil
import subprocess
import pathlib
import time
import types

import pytest

from projectx_desk import regime as R

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"


def walk(n, seed, drift=0.0, vol=1.0, base=100.0):
    rng, p, out = random.Random(seed), base, []
    for i in range(n):
        o = p
        p = max(1.0, p + drift + rng.gauss(0, vol))
        h, l = max(o, p) + abs(rng.gauss(0, vol * 0.3)), min(o, p) - abs(rng.gauss(0, vol * 0.3))
        out.append({"timestamp": i, "open": o, "high": h, "low": l, "close": p, "volume": 1})
    return out


def test_efficiency_line_vs_chop_and_short_input():
    assert R.efficiency([float(i) for i in range(20)]) == pytest.approx(1.0)
    assert R.efficiency([100.0, 101.0] * 10) == pytest.approx(0.0)
    assert R.efficiency([1.0] * 20) is None and R.efficiency([1.0, 2.0]) is None
    assert R.efficiency([1.0] * 12 + [float("nan")]) is None


def test_vwap_crosses_count_only_real_side_changes():
    cl = [101, 99, 101, 99, 101, 101, 101, 101, 101, 101, 101, 101, 101]
    assert R.vwap_cross_count(cl, [100.0] * 13) == 4                    # up, down, up, down inside the 13-point window; the flat tail adds none
    assert R.vwap_cross_count(cl, [100.0] * 13, n=7) == 0               # a shorter window only sees the flat tail
    assert R.vwap_cross_count([101, 99, 101, 99, 101], [100.0] * 5, n=4) == 4
    assert R.vwap_cross_count([101, 102, 103], [100.0] * 3, n=2) == 0
    assert R.vwap_cross_count([101], [None], n=2) is None


def test_run_extent_is_distance_off_the_window_extremes_in_atrs_and_needs_a_full_window():
    hi = [10.0] * 23 + [8.0]
    lo = [9.0] * 23 + [6.0]
    ru, rd = R.run_extent(hi, lo, [7.0] * 24, 2.0)
    assert ru == pytest.approx((7 - 6) / 2.0) and rd == pytest.approx((10 - 7) / 2.0)
    assert R.run_extent(hi[:10], lo[:10], [7.0] * 10, 2.0) == (None, None) and R.run_extent(hi, lo, [7.0] * 24, 0) == (None, None)


def test_tf_regime_labels_trend_and_range_and_unknown():
    trend = R.tf_regime(walk(80, 1, drift=1.2, vol=0.4))
    assert trend["state"] == "TRENDING" and trend["adx"] > 25 and trend["er"] > 0.5 and trend["choppy"] is False
    saw = [{"timestamp": i, "open": 100, "high": 101.0, "low": 99.0, "close": 100.0 + (1 if i % 2 else -1) * 0.5, "volume": 1} for i in range(80)]
    rng = R.tf_regime(saw)
    assert rng["state"] == "RANGING" and rng["choppy"] is True
    short = R.tf_regime(walk(20, 2))
    assert short["state"] == "UNKNOWN" and short["adx"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_twin_matches_python_on_random_bars():
    cases = [walk(n, s, drift=d, vol=v) for n, s, d, v in ((90, 1, 0.8, 0.5), (60, 2, -0.5, 1.0), (45, 3, 0.0, 1.2), (150, 4, 0.1, 0.7))]
    js = ("const R=require(process.argv[1]);const cases=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "console.log(JSON.stringify(cases.map(b=>R.tfRegime(b))))")
    out = subprocess.run(["node", "-e", js, str(STATIC / "regime.js")], input=json.dumps(cases), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    for got, bars in zip(json.loads(out.stdout), cases):
        want = R.tf_regime(bars)
        assert got["state"] == want["state"] and got["choppy"] == want["choppy"]
        assert math.isclose(got["adx"], want["adx"], rel_tol=1e-9) and math.isclose(got["er"], want["er"], rel_tol=1e-9)


def test_bias_log_line_carries_the_regime_fields(monkeypatch):
    import asyncio
    from projectx_desk import app as appmod
    from projectx_desk import digest

    closed = int(time.time() // 300) * 300_000 - 300_000
    ind = {"atr": 5.0, "adx": 30.0, "atr_pctile": 50, "vwap": 100.0, "rsi": 45.0, "ema": {"state": "BEAR", "21": 99.0},
           "closed_ts": closed, "closed_close": 95.0, "reg5": {"er": 0.31, "vwapx": 3, "run_up": 1.2, "run_dn": 4.5}}

    async def fake_ind(app, cid):
        return ind
    monkeypatch.setattr(digest, "indicators", fake_ind)
    monkeypatch.setattr(appmod.px_flow, "summary", lambda *a, **k: None)
    bars15, bars1h = walk(80, 5, drift=0.9, vol=0.4), walk(80, 6, drift=-0.2, vol=1.0)
    st = types.SimpleNamespace(live=types.SimpleNamespace(quotes={}, flow=None),
                               respect_bars={("CON.F.US.MES.Z26", "15m"): {"at": time.monotonic(), "bars": bars15}, ("CON.F.US.MES.Z26", "1h"): {"at": time.monotonic(), "bars": bars1h}})
    e = asyncio.run(appmod._bias_snapshot(types.SimpleNamespace(state=st), "CON.F.US.MES.Z26"))
    assert (e["er5"], e["vwapx"], e["run_up"], e["run_dn"]) == (0.31, 3, 1.2, 4.5)
    assert e["adx15"] > 20 and 0 <= e["er15"] <= 1 and e["adx1h"] is not None and e["er1h"] is not None
    json.dumps(e, allow_nan=False)                                        # numbers or None only, nothing NaN
    st.respect_bars = {k: {**v, "at": time.monotonic() - 3600} for k, v in st.respect_bars.items()}      # the scan stopped an hour ago: unknown, not current
    e2 = asyncio.run(appmod._bias_snapshot(types.SimpleNamespace(state=st), "CON.F.US.MES.Z26"))
    assert e2["adx15"] is None and e2["er1h"] is None                     # nothing cached yet: unknown, never zero


def _ribbon(bars):
    js = ("const R=require(process.argv[1]);const b=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "console.log(JSON.stringify(R.ribbonSlopes(b,[8,13,21,34,55,89],5,0.03)))")
    out = subprocess.run(["node", "-e", js, str(STATIC / "regime.js")], input=json.dumps(bars), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ribbon_slopes_read_up_down_flat_and_a_ribbon_that_is_only_starting_to_turn():
    up = _ribbon(walk(200, 1, drift=0.8, vol=0.3))
    assert up["dirs"] == [1] * 6 and up["arrows"] == "▲" * 6 and up["turn"] is None
    down = _ribbon(walk(200, 2, drift=-0.3, vol=0.15))
    assert down["dirs"] == [-1] * 6 and down["falling"] == 6
    flat = _ribbon([{"timestamp": i, "open": 100, "high": 100.4, "low": 99.6, "close": 100.0, "volume": 1} for i in range(200)])
    assert flat["dirs"] == [0] * 6 and flat["turn"] is None
    bars = walk(150, 3, drift=-0.4, vol=0.2)                                    # a long fall, then a sharp turn up in the last 8 bars
    last = bars[-1]["close"]
    for i in range(8):
        c = last + (i + 1) * 1.6
        bars.append({"timestamp": 150 + i, "open": c - 1.0, "high": c + 0.3, "low": c - 1.2, "close": c, "volume": 1})
    turning = _ribbon(bars)
    assert turning["dirs"][:3] == [1, 1, 1] and turning["turn"] == "up" and -1 in turning["dirs"][3:]
    assert _ribbon(walk(60, 4)) is None                                         # too few bars for the slowest EMA: no claim
