"""Automatic trendlines and channels (static/trend.js, pure). Run under node on synthetic bars: clean structures must be found with honest touch
counts, a broken line must disappear, and a random walk must never yield a line that breaks the module's own rules."""
import json
import math
import random
import shutil
import subprocess
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def bars_from(closes, rng, spread=0.8):
    out = []
    for i, c in enumerate(closes):
        h, l = c + spread + abs(rng.gauss(0, 0.15)), c - spread - abs(rng.gauss(0, 0.15))
        out.append({"timestamp": i, "open": c, "high": h, "low": l, "close": c, "volume": 1})
    return out


def channel(n, slope, amp=4.0, period=24, noise=0.25, seed=1, base=100.0):
    rng = random.Random(seed)
    return bars_from([base + slope * i + amp * math.sin(2 * math.pi * i / period) + rng.gauss(0, noise) for i in range(n)], rng)


def fit(bars, opts=None):
    js = ("const T=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "const r=T.fit(d.bars,d.opts);console.log(JSON.stringify(r))")
    out = subprocess.run(["node", "-e", js, str(STATIC / "trend.js")], input=json.dumps({"bars": bars, "opts": opts}), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_ascending_channel_gives_a_support_a_resistance_a_rail_and_honest_touch_counts():
    r = fit(channel(220, 0.25))
    s, x = r["support"], r["resistance"]
    assert s and x and s["touches"] >= 3 and x["touches"] >= 3
    assert s["slope"] == pytest.approx(0.25, abs=0.06) and x["slope"] == pytest.approx(0.25, abs=0.06)
    assert r["parallel"] is True and r["channel"] is not None and r["channel"]["touches"][1] >= 2
    assert 5.0 < r["channel"]["rail"]["width"] < 12.0                       # the wave is +/- 4 plus the bar ranges
    assert s["breaks"] <= 1 and x["breaks"] <= 1 and r["regression"] is None   # pivot lines found, so no regression fallback


def test_descending_and_flat_structures_are_found_with_the_right_sign():
    d = fit(channel(220, -0.2, base=200.0))
    assert d["support"]["slope"] < -0.1 and d["resistance"]["slope"] < -0.1
    f = fit(channel(220, 0.0))
    assert abs(f["support"]["slope"]) < 0.06 and abs(f["resistance"]["slope"]) < 0.06


def test_a_support_line_price_has_already_crashed_through_is_not_drawn():
    bars = channel(220, 0.25)
    last = bars[-1]["close"]
    atr_guess = 1.6
    for i in range(10):                                                    # a violent break in the last 10 bars
        c = last - (i + 1) * 1.5 * atr_guess
        bars.append({"timestamp": 220 + i, "open": c + 1, "high": c + 1.2, "low": c - 1.2, "close": c, "volume": 1})
    r = fit(bars)
    assert r["support"] is None


def test_a_random_walk_never_yields_a_line_that_breaks_the_modules_own_rules():
    for seed in range(25):
        rng = random.Random(seed)
        p, closes = 100.0, []
        for _ in range(260):
            p += rng.gauss(0, 1.0)
            closes.append(p)
        r = fit(bars_from(closes, rng))
        assert r is not None
        for key in ("support", "resistance"):
            ln = r[key]
            if ln:
                assert ln["touches"] >= 3 and ln["breaks"] <= 1 and ln["lastTouch"] >= r["last"] - 60
        if r["channel"]:
            assert r["channel"]["touches"][1] >= 2 and r["channel"]["rail"]["width"] >= r["atr"]


def test_too_little_or_broken_data_is_no_claim():
    assert fit(channel(30, 0.2)) is None
    bad = channel(100, 0.2)
    bad[50]["high"] = None
    assert fit(bad) is None


def test_regression_is_the_fallback_only_when_it_explains_the_move_and_no_pivot_line_fits():
    rng = random.Random(7)
    ramp = bars_from([100 + 0.4 * i + rng.gauss(0, 1.2) for i in range(160)], rng)
    r = fit(ramp, {"minTouches": 99})                                      # pivot lines disabled: the ramp is explained by the regression
    assert r["support"] is None and r["regression"] is not None and r["regression"]["r2"] > 0.9 and r["regression"]["slope"] > 0.3
    chop = bars_from([100 + rng.gauss(0, 1.5) for _ in range(160)], rng)
    assert fit(chop, {"minTouches": 99})["regression"] is None             # r2 near zero: nothing to draw
