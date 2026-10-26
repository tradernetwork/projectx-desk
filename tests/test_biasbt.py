"""The bias backtest harness: it must reward a real edge, give a coin nothing, and never look backwards or past the horizon."""
import copy
import random

from projectx_desk import biasbt as B

BAR = B.BAR_MS


def rows(prices, lean="LONG", grade="B", atr=1.0, rule="v2.1", c="CON.F.US.MES.Z26", t0=BAR):
    return [{"contract": c, "bar_ts": t0 + i * BAR, "price": p, "atr": atr, "direction": lean, "grade": grade, "rule": rule, "tf": "bias"}
            for i, p in enumerate(prices)]


def test_target_stop_and_time_exit():
    up = rows([100, 100.5, 102, 104] + [104] * 5)                       # +2.25 needs 2.25: bar 3 closes 4 above entry
    assert B.outcome(up, 0, 1) == 1.5 and B.outcome(up, 0, -1) == -1.0
    flat = rows([100] * 9)
    assert B.outcome(flat, 0, 1) == 0.0
    drift = rows([100, 100.3, 100.6, 100.9, 101.2, 101.5, 101.8, 101.8, 101.8])   # +1.8 < 2.25, time exit marks to market: 1.8/1.5
    assert abs(B.outcome(drift, 0, 1, horizon=6) - 1.2) < 1e-9


def test_unscoreable_windows_return_none():
    r = rows([100] * 9)
    del r[3]                                                           # a missing bar inside the window
    assert B.outcome(r, 0, 1) is None
    assert B.outcome(rows([100] * 4), 0, 1) is None                    # runs past the data
    assert B.outcome(rows([100] * 9, atr=0), 0, 1) is None


def test_no_lookahead_the_past_and_the_far_future_cannot_change_an_outcome():
    rng = random.Random(3)
    base = rows([100 + rng.uniform(-3, 3) for _ in range(40)])
    ref = B.outcome(base, 10, 1)
    past, far = copy.deepcopy(base), copy.deepcopy(base)
    for r in past[:10]:
        r["price"] += 50; r["atr"] *= 9
    for r in far[17:]:
        r["price"] -= 50
    assert B.outcome(past, 10, 1) == ref and B.outcome(far, 10, 1) == ref
    for bar, want in ((50, 1.5), (-50, -1.0)):                         # inside the window it MUST matter: the first future bar decides
        moved = copy.deepcopy(base)
        moved[11]["price"] = base[10]["price"] + bar
        assert B.outcome(moved, 10, 1) == want


def test_a_perfect_predictor_scores_and_a_coin_does_not():
    rng = random.Random(11)
    price, seq, leans = 100.0, [], []
    for i in range(600):
        step = rng.choice([-1.0, 1.0])
        seq.append(price); leans.append(step)
        price += step * 0.9
    # "oracle": lean = the sign of the NEXT three bars' drift, so it must beat a coin
    px = []
    p = 100.0
    drifts = [rng.choice([-1, 1]) for _ in range(100)]
    for d in drifts:
        for _ in range(6):
            px.append(p); p += d * 0.8
    orc = rows(px)
    for i, r in enumerate(orc):
        r["direction"] = "LONG" if drifts[min(i // 6, 99)] > 0 else "SHORT"
    tr = B.trades(B.series(orc, "v2.1"))
    assert B.summary([t["r"] for t in tr])["mean_r"] > 0.3 and B.coin_flip_p(tr, ("B",), iters=800) < 0.05
    rnd = rows(px)
    for r in rnd:
        r["direction"] = rng.choice(B.LEANS)
    trr = B.trades(B.series(rnd, "v2.1"))
    assert B.coin_flip_p(trr, ("B",), iters=800) > 0.05 and abs(B.summary([t["r"] for t in trr])["mean_r"]) < 0.3


def test_rule_filter_and_repaint_audit():
    a = rows([100, 101, 102]) + [{**rows([100])[0], "rule": None}]
    assert list(B.series(a, "v2.1").values())[0][0]["rule"] == "v2.1" and len(list(B.series(a, "v2.1").values())[0]) == 3
    twice = rows([100, 101]) + [{**rows([100])[0], "price": 105.0}]    # the same closed bar again, different close
    au = B.audit(twice)
    assert au["repaints"] == 1 and B.audit(rows([100, 101]))["repaints"] == 0
    assert B.audit(a)["mixed_rules"] is True


def test_verdict_says_insufficient_on_a_small_sample_and_report_runs_end_to_end():
    rng = random.Random(5)
    px = [100 + sum(rng.uniform(-1, 1) for _ in range(i % 7)) for i in range(120)]
    rep = B.report(rows(px), iters=200)
    assert rep["verdict"].startswith("INSUFFICIENT") and set(rep["by_grade"]) >= {"A", "B", "C", "D", "A+B", "all leans"}


def test_session_buckets_in_central_time():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ms = lambda y, mo, d, h, mi: int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Chicago")).timestamp() * 1000)
    assert B.session(ms(2026, 10, 25, 17, 5)).startswith("1 sunday reopen")
    assert B.session(ms(2026, 10, 26, 8, 35)).startswith("2 cash open")
    assert B.session(ms(2026, 10, 26, 9, 45)).startswith("3 morning") and B.session(ms(2026, 10, 26, 12, 0)).startswith("4 midday")
    assert B.session(ms(2026, 10, 26, 15, 30)).startswith("5 close") and B.session(ms(2026, 10, 26, 3, 0)).startswith("6 overnight")


def test_regime_tables_skip_fields_the_log_does_not_have_and_read_the_lean_side_run():
    rng = random.Random(9)
    px = [100 + sum(rng.uniform(-1, 1) for _ in range(i % 9)) for i in range(160)]
    rs = rows(px)
    for i, r in enumerate(rs):
        r.update({"direction": "SHORT" if i % 2 else "LONG", "vwap": 100.0, "adx": 15 + i % 20})
        if i >= 80:                                   # only the newer half carries the new fields
            r.update({"er5": (i % 10) / 10, "run_up": 1.0 + i % 5, "run_dn": 9.0})
    tr = B.trades(B.series(rs, "v2.1"))
    tabs = B.regime_tables(tr)
    assert "session" in tabs and "5m ADX" in tabs and "5m efficiency (12 bars)" in tabs
    assert "15m ADX" not in tabs and "1h ADX" not in tabs              # never logged here: no table, not a table of zeros
    short = next(t for t in tr if t["row"]["direction"] == "SHORT" and "run_dn" in t["row"])
    assert B.REGIME_FIELDS["run in lean direction, 24 bars (ATR)"](short["row"], -1) == 9.0     # a SHORT reads run_dn
    assert all(x["n"] >= 1 for x in tabs["5m ADX"])
