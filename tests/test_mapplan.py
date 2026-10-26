"""Shadow-mode dealer-map proposals: triggers on closed bars, the ATR stop (never a map level), advisory targets, conservative grading, and the logger."""
import asyncio
import json
import math
import types
from datetime import datetime

import pytest

from projectx_desk import mapplan as MP
from projectx_desk import mapplan_log as ML
from projectx_desk import tomorrow_feed as F
from projectx_desk.tradeday import CT

TICK = 0.25


def wp(price, role, tail=False):
    return {"price": price, "tag": f"{price:g} {role}", "note": "", "tail": tail}


def a_map(**kw):
    m = {"regime": "negative", "spot": 7700.0, "flip": 7770.0, "pin": 7700.0, "typical_move": 70.0, "flip_on_price": False,
         "roads": [{"key": "UP", "waypoints": [wp(7770, "FLIP"), wp(7730, "ACCELERANT"), wp(7745, "BRAKE")]},
                   {"key": "DOWN", "waypoints": [wp(7690, "TRAPDOOR"), wp(7640, "PUT WALL")]},
                   {"key": "RANGE", "waypoints": [wp(7700, "PIN")], "band": {"lo": 7690, "hi": 7730}}]}
    m.update(kw)
    return m


def bar(i, close, high=None, low=None, rng=2.0):
    return {"timestamp": 1_000_000 + i * MP.BAR_MS, "open": close, "high": close + rng / 2 if high is None else high, "low": close - rng / 2 if low is None else low, "close": close}


def series(closes, rng=2.0):
    return [bar(i, c, rng=rng) for i, c in enumerate(closes)]


def det(closes, atr=4.0, m=None, rng=2.0, **kw):
    return MP.detect(m or a_map(), series(closes, rng), atr, TICK, root="MES", contract="CON.F.US.MES.Z26", **kw)


FLAT = [7700.0] * 20


def test_a_close_through_a_trapdoor_proposes_a_short_with_an_atr_stop_and_an_advisory_target():
    ps = det(FLAT + [7692.0, 7688.0])
    p = next(x for x in ps if x["kind"] == "TRAPDOOR_LOST")
    assert (p["side"], p["entry"], p["level"], p["role"]) == ("SHORT", 7688.0, 7690.0, "TRAPDOOR")
    assert p["stop_dist"] == 6.0 and p["stop"] == 7694.0 and p["stop_ticks"] == 24           # 1.5 x ATR, rounded UP to a tick, above entry
    assert p["target"] == 7640.0 and p["target_role"] == "PUT WALL" and p["r_target"] == 8.0      # the next level beyond 1R, advisory only


def test_the_stop_is_never_a_map_level_only_the_atr_distance():
    p = det(FLAT + [7692.0, 7688.0])[0]
    levels = {7770, 7730, 7745, 7690, 7640, 7700}
    assert p["stop"] not in levels and p["stop"] == pytest.approx(p["entry"] + 1.5 * 4.0)


def test_each_trigger_fires_on_the_right_event_and_only_on_the_closed_bar():
    assert {p["kind"] for p in det(FLAT + [7728.0, 7731.5])} == {"ACCEL_THROUGH"}                      # close up through the accelerant: long
    p = next(x for x in det(FLAT + [7740.0, 7746.0]) if x["kind"] == "BRAKE_CLEARED")
    assert p["side"] == "LONG" and p["stop"] == 7746.0 - 6.0
    rej = det(FLAT + [7760.0, 7762.0], atr=4.0)
    assert not any(x["kind"] == "FLIP_REJECT" for x in rej)                                              # never touched the flip
    s = series(FLAT + [7760.0, 7762.0])
    s[-1]["high"] = 7771.0                                                                                 # wick through the flip, closed back under it
    assert any(x["kind"] == "FLIP_REJECT" and x["side"] == "SHORT" for x in MP.detect(a_map(), s, 4.0, TICK, root="MES"))
    s2 = series(FLAT[:-1] + [7692.0, 7689.0, 7691.5])                                                      # lost it, then closed back over: a failed breakdown
    assert any(x["kind"] == "TRAPDOOR_RECLAIM" and x["side"] == "LONG" for x in MP.detect(a_map(), s2, 4.0, TICK, root="MES"))


def test_nothing_fires_without_a_cross_and_a_pin_is_never_a_trigger_or_a_target():
    assert det(FLAT + [7695.0, 7694.0]) == []                                                              # drifting under no level
    ps = det(FLAT + [7692.0, 7688.0], m=a_map(roads=[{"key": "DOWN", "waypoints": [wp(7690, "TRAPDOOR")]}, {"key": "RANGE", "waypoints": [wp(7650, "PIN")]}]))
    assert ps and ps[0]["target"] is None and ps[0]["r_target"] is None                                  # the only level below is a pin: no target, not an invented one


def test_a_plan_the_guard_would_refuse_is_not_proposed_and_garbage_yields_nothing():
    assert det(FLAT + [7692.0, 7688.0], atr=60.0) == []                                                    # 360 ticks: over the 200 cap, not clamped
    assert det(FLAT + [7692.0, 7688.0], atr=0.3) == []                                                     # under 4 ticks
    for bad_atr in (None, 0, -1, float("nan"), True):
        assert det(FLAT + [7692.0, 7688.0], atr=bad_atr) == []
    assert MP.detect(a_map(), series(FLAT + [7692.0, 7688.0]), 4.0, 0, root="MES") == [] and MP.detect(None, series(FLAT), 4.0, TICK) == []
    s = series(FLAT + [7692.0, 7688.0])
    s[-1]["close"] = float("nan")
    assert MP.detect(a_map(), s, 4.0, TICK, root="MES") == []


def test_the_flip_below_price_is_a_level_even_though_it_is_not_a_waypoint():
    m = a_map(spot=7800.0, flip=7770.0, roads=[{"key": "DOWN", "waypoints": [wp(7640, "PUT WALL")]}])
    assert {"role": "FLIP", "price": 7770.0, "tail": True} in MP.levels(m) or any(l["role"] == "FLIP" for l in MP.levels(m))


def test_cooldown_daily_cap_and_duplicates():
    p = det(FLAT + [7692.0, 7688.0])[0]
    cd = lambda ms: ML.ct_date(ms)
    assert MP.allowed(p, [], 0, cd) is True
    prior = {**p, "kind_row": "proposal", "bar_ts": p["bar_ts"] - 5 * MP.BAR_MS, "id": "old"}
    assert MP.allowed(p, [prior], 0, cd) is False                                                          # same kind and level inside 30 minutes
    far = {**prior, "bar_ts": p["bar_ts"] - 8 * MP.BAR_MS}
    assert MP.allowed(p, [far], 0, cd) is True
    cap = [{**p, "kind_row": "proposal", "id": f"x{i}", "kind": f"K{i}", "bar_ts": p["bar_ts"] - (i + 1) * 100 * MP.BAR_MS} for i in range(MP.DAILY_CAP)]
    assert MP.allowed(p, cap, 0, lambda ms: "2026-10-27") is False                                         # a daily cap, so chop cannot become a tilt machine


def short_plan(**kw):
    return {"id": "t", "root": "MES", "kind": "TRAPDOOR_LOST", "side": "SHORT", "entry": 7688.0, "stop": 7694.0, "stop_dist": 6.0, "target": 7640.0, **kw}


def after(*triples):
    return [{"timestamp": 2_000_000 + i * MP.BAR_MS, "high": h, "low": l, "close": c} for i, (h, l, c) in enumerate(triples)]


def test_grading_stop_target_both_in_one_bar_timeout_and_pending():
    stop = MP.resolve(short_plan(), after((7696.0, 7686.0, 7694.0)))
    assert (stop["outcome"], stop["r"]) == ("STOP", -1.0) and stop["r_opp"] > 0                           # the opposite side did fine: that is the baseline
    tgt = MP.resolve(short_plan(), after((7690.0, 7660.0, 7662.0), (7665.0, 7639.0, 7642.0)))
    assert (tgt["outcome"], tgt["r"], tgt["bars"]) == ("TARGET", 8.0, 2) and tgt["mfe_r"] > 7
    both = MP.resolve(short_plan(), after((7695.0, 7639.0, 7680.0)))
    assert both["outcome"] == "STOP" and both["both_in_bar"] is True                                       # conservative: a bar touching both is the stop
    notarget = MP.resolve(short_plan(target=None), after(*[(7690.0, 7684.0, 7686.0)] * 24))
    assert notarget["outcome"] == "TIMEOUT" and notarget["bars"] == 24 and notarget["r"] == pytest.approx((7688 - 7686) / 6, abs=1e-3)
    assert MP.resolve(short_plan(), after((7690.0, 7684.0, 7686.0))) is None and MP.resolve(short_plan(), []) is None   # not enough bars yet: pending


def test_a_long_is_graded_with_the_signs_flipped_and_flat_by_ends_the_trade():
    lp = {"id": "l", "root": "MES", "kind": "ACCEL_THROUGH", "side": "LONG", "entry": 7731.0, "stop": 7725.0, "stop_dist": 6.0, "target": 7770.0}
    assert MP.resolve(lp, after((7740.0, 7732.0, 7738.0), (7771.0, 7735.0, 7770.0)))["outcome"] == "TARGET"
    assert MP.resolve(lp, after((7733.0, 7724.0, 7726.0)))["outcome"] == "STOP"
    cutoff = 2_000_000 + 2 * MP.BAR_MS
    fb = MP.resolve(short_plan(), after((7690.0, 7684.0, 7686.0), (7690.0, 7684.0, 7685.0), (7690.0, 7684.0, 7684.0)), flat_by_ms=lambda ts: cutoff)
    assert fb["outcome"] == "FLATBY" and fb["r"] == pytest.approx((7688 - 7685) / 6, abs=1e-3)


def test_no_rate_is_shown_under_thirty_resolved_per_kind():
    props = [{**short_plan(), "id": f"p{i}"} for i in range(31)]
    outs = [{"id": f"p{i}", "outcome": "STOP" if i % 2 else "TARGET", "r": -1.0 if i % 2 else 2.0, "r_opp": 0.5} for i in range(29)]
    s = MP.stats(props, outs)[0]
    assert (s["proposed"], s["resolved"], s["enough"], s["mean_r"]) == (31, 29, False, None)
    outs += [{"id": "p29", "outcome": "TARGET", "r": 2.0, "r_opp": 0.5}]
    s = MP.stats(props, outs)[0]
    assert s["enough"] is True and s["mean_r"] == pytest.approx((16 * 2.0 - 14 * 1.0) / 30, abs=1e-3) and s["mean_r_opp"] == 0.5


# ---------------------------------------------------------------- the logger (async, with fakes)
NOW = datetime(2026, 10, 27, 10, 3, tzinfo=CT).timestamp()


def fresh_bars(closes, rng=4.0):
    last_start = (NOW - 30) * 1000 - MP.BAR_MS                      # the trigger bar closed 30 s ago
    n = len(closes)
    return [{"timestamp": last_start - (n - 1 - i) * MP.BAR_MS, "open": c, "high": c + rng / 2, "low": c - rng / 2, "close": c, "volume": 1} for i, c in enumerate(closes)]


def fake_app():
    return types.SimpleNamespace(state=types.SimpleNamespace(tracked={"MES": "CON.F.US.MES.Z26"}))


@pytest.fixture
def ok_map(monkeypatch):
    async def fake(app, cid, now=None, **kw):
        return {"status": "OK", "root": "MES", "age_s": 60, "snapshot_epoch": NOW - 60, "map": {**a_map(), "version": 1}}
    monkeypatch.setattr(F, "tomorrow_result", fake)


def run_tick(path, closes, now=NOW):
    async def bars_fn(app, cid):
        return fresh_bars(closes)
    asyncio.run(ML.tick(fake_app(), path, now, bars_fn=bars_fn))


def test_a_trigger_on_the_just_closed_bar_is_filed_once_and_later_graded(tmp_path, ok_map):
    p = tmp_path / "m.jsonl"
    closes = [7700.0] * 30 + [7692.0, 7688.0]
    run_tick(p, closes)
    run_tick(p, closes)                                                # the same bar again: nothing new
    rows = ML.load(p)
    props = [r for r in rows if r["kind_row"] == "proposal"]
    assert len(props) == 1 and props[0]["kind"] == "TRAPDOOR_LOST" and props[0]["map_version"] == 1 and props[0]["stop_ticks"] > 0
    # later: 24 quiet bars after the trigger resolve it as a timeout
    trigger_ts = props[0]["bar_ts"]
    later = NOW + 25 * 300

    async def bars_fn(app, cid):
        base = fresh_bars(closes)
        return base + [{"timestamp": trigger_ts + (i + 1) * MP.BAR_MS, "open": 7686.0, "high": 7688.0, "low": 7684.0, "close": 7686.0, "volume": 1} for i in range(25)]
    asyncio.run(ML.tick(fake_app(), p, later, bars_fn=bars_fn))
    outs = [r for r in ML.load(p) if r["kind_row"] == "outcome"]
    assert len(outs) == 1 and outs[0]["id"] == props[0]["id"] and outs[0]["outcome"] in ("TIMEOUT", "FLATBY", "TARGET", "STOP")


def test_an_old_bar_or_yesterdays_map_or_a_failed_map_files_nothing(tmp_path, monkeypatch, ok_map):
    p = tmp_path / "m.jsonl"
    closes = [7700.0] * 30 + [7692.0, 7688.0]
    run_tick(p, closes, now=NOW + 400)                                 # the trigger bar closed over six minutes ago: stale
    assert ML.load(p) == []

    async def yesterday(app, cid, now=None, **kw):
        return {"status": "OK", "root": "MES", "age_s": 60, "snapshot_epoch": NOW - 86400, "map": a_map()}
    monkeypatch.setattr(F, "tomorrow_result", yesterday)
    run_tick(p, closes)
    assert ML.load(p) == []

    async def failed(app, cid, now=None, **kw):
        return {"status": "UNAVAILABLE", "code": "NO_REF", "map": None}
    monkeypatch.setattr(F, "tomorrow_result", failed)
    run_tick(p, closes)
    assert ML.load(p) == []


def test_the_log_survives_garbage_and_the_shadow_modules_cannot_reach_the_order_path(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('junk\n[1]\n{"kind_row":"other"}\n', encoding="utf-8")
    assert ML.load(p) == [] and ML.load(tmp_path / "nope.jsonl") == []
    import ast
    import pathlib
    for name in ("mapplan.py", "mapplan_log.py"):
        tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / name).read_text(encoding="utf-8"))
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                imported |= {n.module or ""} | {a.name for a in n.names}
            elif isinstance(n, ast.Import):
                imported |= {a.name for a in n.names}
        assert not imported & {"guard", "client", "hub", "projectx_desk.guard", "projectx_desk.client", "projectx_desk.hub", "mcp", "state"}, (name, imported)
        called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not called & {"place", "preview", "flatten", "write", "move_stop", "stage"}, (name, called)      # the same names the order-path pin forbids elsewhere


def test_ema21_rejection_and_hold_are_shadow_events_with_atr_stops_and_a_per_kind_cooldown():
    ema = [{"price": 7702.0, "role": "EMA21"}]
    s = series(FLAT + [7699.0, 7700.0])
    s[-1]["high"] = 7703.0                                                  # wick through the 21 EMA, closed back under it
    rej = [x for x in MP.detect(a_map(), s, 4.0, TICK, root="MES", extra_levels=ema) if x["role"] == "EMA21"]
    assert [(x["kind"], x["side"], x["level"]) for x in rej] == [("EMA21_REJECT", "SHORT", 7702.0)]
    assert rej[0]["stop"] == 7700.0 + 6.0 and rej[0]["target"] != 7702.0     # ATR stop, and the EMA is never a target
    s2 = series(FLAT + [7705.0, 7701.0])
    s2[-1]["low"] = 7699.5                                                  # dipped through, closed back over
    hold = [x for x in MP.detect(a_map(), s2, 4.0, TICK, root="MES", extra_levels=[{"price": 7700.0, "role": "EMA21"}]) if x["role"] == "EMA21"]
    assert [(x["kind"], x["side"]) for x in hold] == [("EMA21_HOLD", "LONG")]
    assert not any(x["role"] == "EMA21" for x in MP.detect(a_map(), s, 4.0, TICK, root="MES"))     # without the extra level it does not exist
    assert not [x for x in MP.detect(a_map(), s, 4.0, TICK, root="MES", extra_levels=[{"price": 7702.0, "role": "FLIP"}]) if x["level"] == 7702.0]   # only the EMA21 role is accepted as an extra level
    # the EMA moves every bar: a second rejection 10 minutes later at a different level is still inside the cooldown
    first = {**rej[0], "kind_row": "proposal"}
    later = {**rej[0], "level": 7704.5, "bar_ts": rej[0]["bar_ts"] + 600_000, "id": "other"}
    cd = lambda ms: "d"
    assert MP.allowed(later, [first], 0, cd) is False
    assert MP.allowed({**later, "bar_ts": rej[0]["bar_ts"] + 31 * 60_000}, [first], 0, cd) is True
