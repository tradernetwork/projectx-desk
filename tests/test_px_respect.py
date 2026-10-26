import json
import math
import random
import time

import pytest

from projectx_desk import respect as R
T0 = 1_700_000_000_000


def _timed(fn, *a, **kw):
    """Seconds fn took to run. Used by the timing test, which takes the best of a few samples."""
    t = time.perf_counter()
    fn(*a, **kw)
    return time.perf_counter() - t


def walk(n, seed, step=900_000):
    rng = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        o = p
        p += rng.gauss(0, 1)
        out.append({"timestamp": T0 + i * step, "open": o, "high": max(o, p) + abs(rng.gauss(0, .4)),
                    "low": min(o, p) - abs(rng.gauss(0, .4)), "close": p, "volume": 100 + i % 7})
    return out


def ema_bouncer(n=1000, seed=3, period=34, step=3_600_000):
    """Price runs up away from its own EMA, pulls back, touches it, and bounces. Closed loop on the live EMA."""
    rng = random.Random(seed)
    k = 2.0 / (period + 1)
    closes = [100.0 + 0.1 * i for i in range(period)]
    e = sum(closes) / period
    p, phase, out = closes[-1], "run", []
    for i in range(period):
        c = closes[i]
        out.append({"timestamp": T0 + i * step, "open": c, "high": c + .5, "low": c - .5, "close": c, "volume": 10})
    for i in range(period, n):
        o = p
        if phase == "run":
            p += 0.6 + rng.uniform(-.1, .1)
            if p - e > 4:
                phase = "pull"
        else:
            p -= 0.6 + rng.uniform(-.1, .1)
            if p - e < 0.5:
                p = e + 0.15          # tags the EMA, closes just above it
                phase = "run"
        low = min(o, p) - 0.15
        if phase == "run" and p == e + 0.15:
            low = e - 0.02
        out.append({"timestamp": T0 + i * step, "open": o, "high": max(o, p) + 0.15, "low": low, "close": p, "volume": 10})
        e = p * k + e * (1 - k)
    return out


def test_ema34_bouncer_ranks_ema34_respected_and_stable():
    r = R.scan({"1h": ema_bouncer()}, top=5)["tfs"]["1h"]
    assert r["tested"] >= 20 and r["note"] is None
    ids = [x["id"] for x in r["levels"]]
    assert "ema34" in ids
    row = next(x for x in r["levels"] if x["id"] == "ema34")
    assert row["verdict"] == R.RESPECTED and row["stable"] is True and row["tests"] >= 30
    assert row["hold_rate"] - row["placebo_rate"] >= 15
    assert row["family"] == "ema" and row["label"] == "EMA 34"
    assert r["levels"][0]["verdict"] == R.RESPECTED


def test_random_walks_rarely_produce_stable_respected_levels():
    respected = stable = total = 0
    for seed in range(4):
        for tf in ("15m", "1h"):
            for x in R.scan({tf: walk(1000, seed)}, top=99)["tfs"][tf]["levels"]:
                total += 1
                respected += x["verdict"] == R.RESPECTED
                stable += x["verdict"] == R.RESPECTED and x["stable"] is True
    assert total > 100
    assert respected / total < 0.10          # luck exists, but the surrogate calibration keeps it small
    assert stable <= 2


def test_bollinger_has_no_free_edge_on_random_walk():
    bb = [x for seed in range(6) for x in R.scan({"1h": walk(1000, seed + 50)}, top=99)["tfs"]["1h"]["levels"]
          if x["family"] == "boll"]
    assert sum(x["verdict"] == R.RESPECTED for x in bb) <= 2


def test_no_lookahead_prefix_values_identical_when_truncated():
    bars = R.normalize(walk(700, 11))
    for tf in ("15m", "1d"):
        full = R.build_levels(bars, tf)
        assert {"vwap"} <= set(full) or tf == "1d"
        for cut in (250, 401, 650):
            part = R.build_levels(bars[:cut], tf)
            assert set(part) <= set(full)
            for cid, series in part.items():
                assert series == full[cid][:cut], (tf, cid, cut)


def test_no_lookahead_pivots_wait_for_confirmation():
    # one huge spike high at bar 30; fib levels must not exist from it until bar 30 + lookback
    bars = R.normalize([{"o": 100, "h": 101, "l": 99, "c": 100, "v": 1, "t": T0 + i * 60000} for i in range(60)])
    bars[20]["l"] = 90.0
    bars[30]["h"] = 120.0
    fb = R.fib_series(bars, lookback=5)[0.5]
    assert all(v is None for v in fb[:35])
    assert fb[35] == pytest.approx(105.0)       # (120 + 90) / 2, direction: low then high, so measured down from the high


def test_levels_defined_and_correct():
    bars = R.normalize(walk(300, 5))
    lv = R.build_levels(bars, "1h")
    closes = [b["c"] for b in bars]
    assert lv["sma20"][19] == pytest.approx(sum(closes[:20]) / 20) and lv["sma20"][18] is None
    assert lv["bb_up"][-1] > lv["bb_mid"][-1] > lv["bb_lo"][-1]
    assert lv["kc_up20"][-1] > lv["kc_up15"][-1] > lv["kc_mid"][-1] > lv["kc_lo15"][-1] > lv["kc_lo20"][-1]
    assert set(lv) <= set(R.CATALOG)


def test_prior_day_levels_use_only_completed_periods():
    # three 17:00 CT sessions of 1h bars with distinct ranges
    bars, step = [], 3_600_000
    start = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000) + 23 * 3_600_000    # 23:00 UTC = 17:00 CST
    for d in range(3):
        for h in range(24):
            base = 100 + d * 10
            bars.append({"o": base, "h": base + 1 + h * 0.01, "l": base - 1, "c": base + 0.5, "v": 1,
                         "t": start + (d * 24 + h) * step})
    nb = R.normalize(bars)
    pdh, pdl, pdc = R.prior_series(nb, weekly=False)
    assert pdh[5] is None                           # first session has no prior
    assert pdh[30] == pytest.approx(101.23) and pdl[30] == 99 and pdc[30] == 100.5
    assert pdh[60] == pytest.approx(111.23) and pdl[60] == 109


def test_vwap_resets_each_session_and_needs_volume():
    bars = R.normalize(walk(120, 2, step=3_600_000))
    vw = R.vwap_series(bars)
    assert vw is not None and sum(v is not None for v in vw) > 100
    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    assert all(lo <= v <= hi for v in vw if v is not None)
    nov = [dict(b, v=0.0) for b in bars]
    assert R.vwap_series(nov) is None
    assert "vwap" not in R.build_levels(R.normalize(walk(300, 2)), "1d")       # daily never gets VWAP
    notime = [dict(b, t=None) for b in bars]
    assert R.vwap_series(notime) is None


def test_short_missing_and_garbage_input():
    r = R.scan({"5m": walk(10, 1), "1h": None, "4h": "junk", "1d": [None, 3, {"x": 1}]})
    assert set(r["tfs"]) == set(R.TIMEFRAMES)
    for tf in R.TIMEFRAMES:
        assert r["tfs"][tf]["levels"] == [] and r["tfs"][tf]["note"] == "TOO_FEW_BARS" and r["tfs"][tf]["tested"] == 0
    assert R.scan(None)["tfs"]["5m"]["note"] == "TOO_FEW_BARS"
    assert R.scan({})["v"] == 1


def test_nan_inf_none_bars_are_dropped_not_propagated():
    bars = walk(400, 8)
    bars[10]["close"] = float("nan")
    bars[20]["high"] = float("inf")
    bars[30]["low"] = None
    bars[40]["open"] = "abc"
    bars[50]["volume"] = float("nan")
    bars[60]["timestamp"] = "garbage"
    r = R.scan({"1h": bars}, top=99)
    assert r["tfs"]["1h"]["bars"] == 396
    json.dumps(r, allow_nan=False)


def test_accepts_both_key_styles_and_iso_timestamps():
    a = walk(300, 4)
    b = [{"o": x["open"], "h": x["high"], "l": x["low"], "c": x["close"], "v": x["volume"],
          "t": "2023-11-14T22:13:20Z" if i == 0 else x["timestamp"]} for i, x in enumerate(a)]
    na, nb = R.normalize(a), R.normalize(b)
    assert len(na) == len(nb) == 300
    assert na[5] == nb[5]
    assert R.normalize([{"o": 1, "h": 2, "l": 0, "c": 1, "t": 1_700_000_000}])[0]["t"] == 1_700_000_000_000   # epoch seconds


def test_output_shape_json_and_ordering():
    r = R.scan({tf: ema_bouncer(seed=i) for i, tf in enumerate(R.TIMEFRAMES)}, top=4)
    s = json.dumps(r, allow_nan=False)
    assert json.loads(s) == r
    assert r["v"] == 1
    keys = {"id", "family", "label", "tests", "held", "failed", "neutral", "hold_rate", "placebo_rate", "verdict",
            "stable", "price", "p_value", "fdr"}
    for tf, t in r["tfs"].items():
        assert set(t) == {"bars", "tested", "note", "family", "levels"} and len(t["levels"]) <= 4
        assert t["family"] >= t["tested"] > 0                                # one family for the whole scan
        prev = None
        for x in t["levels"]:
            assert set(x) == keys
            assert x["id"] in R.CATALOG and x["family"] == R.CATALOG[x["id"]][0] and x["label"] == R.CATALOG[x["id"]][1]
            assert x["held"] + x["failed"] + x["neutral"] == x["tests"] > 0
            assert x["stable"] in (True, False, None)
            assert x["fdr"] in (True, False, None) and (x["p_value"] is None or 0.0 <= x["p_value"] <= 1.0)
            if x["verdict"] == R.RESPECTED:
                assert x["fdr"] is True and x["tests"] >= R.MIN_N and x["hold_rate"] - x["placebo_rate"] >= 15
            assert x["price"] is None or math.isfinite(x["price"])
            edge = x["hold_rate"] - x["placebo_rate"] if x["hold_rate"] is not None and x["placebo_rate"] is not None else -1e9
            k = (x["verdict"] == R.RESPECTED, edge, x["tests"])
            assert prev is None or prev >= k
            prev = k
    assert R.scan({"1h": walk(300, 1)}, top=0)["tfs"]["1h"]["levels"] == []


def test_hold_rate_matches_px_levels_directly():
    from projectx_desk import levels as L
    bars = R.normalize(ema_bouncer())
    c = [b["c"] for b in bars]
    atr = L.atr_series([b["h"] for b in bars], [b["l"] for b in bars], c, 14)
    st = L.bounce_stats(bars, L.ema_series(c, 34), atr, max_events=10 ** 9)
    row = next(x for x in R.scan({"1h": bars}, top=99)["tfs"]["1h"]["levels"] if x["id"] == "ema34")
    assert (row["tests"], row["held"], row["failed"], row["neutral"], row["hold_rate"]) == \
        (st["n"], st["bounced"], st["failed"], st["neutral"], st["rate"])


def test_stable_none_when_halves_too_small():
    r = R.scan({"1h": ema_bouncer(n=130)}, top=99)["tfs"]["1h"]
    assert r["levels"] and all(x["stable"] in (None, False) for x in r["levels"])
    assert any(x["stable"] is None for x in r["levels"])


def test_deterministic():
    d = {"1h": walk(500, 9)}
    assert R.scan(d) == R.scan(d)


def test_speed_five_timeframes_of_1000_bars():
    d = {tf: walk(1000, i) for i, tf in enumerate(R.TIMEFRAMES)}
    # Best of 3: a single sample tripped the 2.0 s bar on a loaded home server (2.13 s) and passed on the rerun.
    # The bar stays where it is; only the load noise goes away. A real slowdown misses it three times.
    best = min(_timed(R.scan, d) for _ in range(3))
    assert best < 2.0
