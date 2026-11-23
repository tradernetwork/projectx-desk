"""Trade-event detector: the five habits, shared definitions with journal.py, silent baseline, freshness, fixed templates, config, the pin."""
import ast
import pathlib
import re
from datetime import datetime, timezone

import pytest

from projectx_desk import journal as j
from projectx_desk import tradewatch as tw

ROOT = pathlib.Path(__file__).resolve().parent.parent
MES = "CON.F.US.MES.Z26"
T0 = int(datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)      # 10:00 CT
LATE = int(datetime(2026, 10, 29, 20, 2, tzinfo=timezone.utc).timestamp() * 1000)    # 15:02 CT


def fill(i, ms, side, price=7700.0, size=1, pnl=None, cid=MES):
    return {"id": i, "accountId": 4242, "contractId": cid, "orderId": i, "price": price, "size": size, "side": side, "fees": 0.37,
            "profitAndLoss": pnl, "voided": False, "creationTimestamp": datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()}


def pos(side=1, size=1, cid=MES):
    return {"contractId": cid, "type": side, "size": size, "averagePrice": 7700.0}


def stop(side=1, size=1, cid=MES, typ=4):
    return {"id": 9, "contractId": cid, "type": typ, "side": side, "size": size, "status": 1, "stopPrice": 7690.0}


def new(bias=None, nostop=15):
    return tw.TradeWatch(nostop, (lambda: bias) if bias is not None else None)


def step(w, fills, positions=(), orders=(), now_ms=T0, now_s=0.0, seeded=True):
    return w.step(list(fills), list(positions), list(orders), seeded, now_s, now_ms)


def kinds(evs):
    return [e["kind"] for e in evs]


# ---- baseline and freshness ----------------------------------------------------------------------------------------------

def test_first_seeded_look_is_a_silent_baseline_even_with_a_late_open_position():
    w = new()
    assert step(w, [fill(1, LATE - 5000, 0)], [pos()], now_ms=LATE) == []
    assert w.snapshot(LATE)["status"] == "OK"


def test_nothing_is_announced_or_primed_while_unseeded():
    w = new()
    assert step(w, [fill(1, T0, 0)], seeded=False) == [] and w.snapshot(T0)["status"] == "WAIT"


def test_a_fill_that_arrives_late_with_an_old_timestamp_is_not_announced():
    w = new()
    step(w, [], now_ms=T0)
    assert step(w, [fill(1, T0 - 3_600_000, 0)], [pos()], [stop()], now_ms=T0) == []


# ---- the five habits -----------------------------------------------------------------------------------------------------

def test_no_stop_fires_once_after_n_seconds_and_resets_when_covered_or_flat():
    w = new(nostop=15)
    step(w, [], now_ms=T0)
    assert step(w, [fill(1, T0, 0)], [pos()], now_ms=T0, now_s=100.0) == []
    assert step(w, [fill(1, T0, 0)], [pos()], now_ms=T0 + 14_000, now_s=114.0) == []
    ev = step(w, [fill(1, T0, 0)], [pos()], now_ms=T0 + 15_000, now_s=115.0)
    assert kinds(ev) == ["NO_STOP"] and ev[0]["text"] == "MES LONG has no working stop order 15s after it was seen open"
    assert step(w, [fill(1, T0, 0)], [pos()], now_ms=T0 + 30_000, now_s=130.0) == []          # once per episode
    step(w, [fill(1, T0, 0)], [pos()], [stop()], now_ms=T0 + 31_000, now_s=131.0)                 # covered: episode over
    step(w, [fill(1, T0, 0)], [pos()], now_ms=T0 + 32_000, now_s=132.0)
    assert kinds(step(w, [fill(1, T0, 0)], [pos()], now_ms=T0 + 50_000, now_s=150.0)) == ["NO_STOP"]


def test_a_stop_on_the_wrong_side_or_too_small_or_a_different_contract_does_not_count():
    for o in (stop(side=0), stop(size=1, cid="CON.F.US.MNQ.Z26"), {**stop(), "status": 3}):
        w = new(nostop=3)
        step(w, [], [pos(size=2)], [o], now_ms=T0, now_s=0.0)
        assert kinds(step(w, [], [pos(size=2)], [o], now_ms=T0 + 4000, now_s=4.0)) == ["NO_STOP"]
    w = new(nostop=3)
    step(w, [], [pos(size=2)], [stop(size=1)], now_ms=T0, now_s=0.0)                                 # too small
    assert kinds(step(w, [], [pos(size=2)], [stop(size=1)], now_ms=T0 + 4000, now_s=4.0)) == ["NO_STOP"]


def test_a_trailing_stop_counts_as_a_stop_like_everywhere_else():
    w = new(nostop=3)
    step(w, [], [pos()], [stop(typ=5)], now_ms=T0, now_s=0.0)
    assert step(w, [], [pos()], [stop(typ=5)], now_ms=T0 + 9000, now_s=9.0) == []


def test_reentry_within_ten_minutes_of_a_losing_exit_uses_journals_window():
    w = new()
    base = [fill(1, T0, 0), fill(2, T0 + 60_000, 1, pnl=-80.0)]
    step(w, base, now_ms=T0 + 61_000)
    t = T0 + 61_000 + j.REVENGE_S * 1000 - 1000 - 61_000          # 9m59s after the exit's timestamp
    ev = step(w, base + [fill(3, T0 + 60_000 + j.REVENGE_S * 1000 - 1000, 1, price=7700.0)], now_ms=T0 + 60_000 + j.REVENGE_S * 1000)
    assert "REENTRY" in kinds(ev) and "9m 59s after a losing exit on MES" in ev[0]["text"] and ev[0]["text"].startswith("MES SHORT entry")
    assert t > 0


def test_reentry_outside_the_window_or_after_a_win_is_silent():
    w = new()
    base = [fill(1, T0, 0), fill(2, T0 + 60_000, 1, pnl=-80.0)]
    step(w, base, now_ms=T0 + 61_000)
    gap = T0 + 60_000 + j.REVENGE_S * 1000 + 1000
    assert "REENTRY" not in kinds(step(w, base + [fill(3, gap, 0)], now_ms=gap))
    w = new()
    win = [fill(1, T0, 0), fill(2, T0 + 60_000, 1, pnl=80.0)]
    step(w, win, now_ms=T0 + 61_000)
    assert step(w, win + [fill(3, T0 + 90_000, 0)], now_ms=T0 + 90_000) == []


def test_scale_in_fires_for_the_second_entry_but_not_for_a_partial_exit():
    w = new()
    f = [fill(1, T0, 0, size=2)]
    step(w, f, [pos(size=2)], [stop(size=2)], now_ms=T0)
    f2 = f + [fill(2, T0 + 30_000, 0)]
    ev = step(w, f2, [pos(size=3)], [stop(size=3)], now_ms=T0 + 30_000)
    assert kinds(ev) == ["SCALE_IN"] and ev[0]["text"] == "MES LONG added to: entry 2 in the same position"
    f3 = f2 + [fill(3, T0 + 60_000, 1, pnl=10.0)]
    assert step(w, f3, [pos(size=2)], [stop(size=2)], now_ms=T0 + 60_000) == []


def test_late_entry_after_1455_central_and_not_just_before():
    w = new()
    step(w, [], now_ms=LATE)
    ev = step(w, [fill(1, LATE, 0)], now_ms=LATE)
    assert kinds(ev) == ["LATE_ENTRY"] and ev[0]["text"] == "MES LONG entry at 15:02 CT, after 14:55"
    w = new()
    early = int(datetime(2026, 10, 29, 19, 54, tzinfo=timezone.utc).timestamp() * 1000)      # 14:54 CT
    step(w, [], now_ms=early)
    assert step(w, [fill(1, early, 0)], now_ms=early) == []


def test_counter_bias_uses_the_last_read_at_or_before_the_entry_never_after():
    bias = {MES: [{"ts": T0 - 120_000, "grade": "B", "direction": "LONG", "score": 2.0, "adx": 25.0, "flags": []},
                  {"ts": T0 + 5_000, "grade": "A", "direction": "SHORT", "score": 3.0, "adx": 25.0, "flags": []}]}
    w = new(bias)
    step(w, [], now_ms=T0)
    ev = step(w, [fill(1, T0, 1)], now_ms=T0)                             # SHORT entry while the standing read is LONG
    assert kinds(ev) == ["COUNTER_BIAS"] and ev[0]["text"] == "MES SHORT entry against the BIAS read (LONG, grade B)"
    w = new(bias)
    step(w, [], now_ms=T0)
    assert step(w, [fill(1, T0, 0)], now_ms=T0) == []                     # with the read: nothing
    stale = {MES: [{"ts": T0 - 3_600_000, "grade": "B", "direction": "LONG", "score": 2.0, "adx": 25.0, "flags": []}]}
    w = new(stale)
    step(w, [], now_ms=T0)
    assert step(w, [fill(1, T0, 1)], now_ms=T0) == []                     # an hour-old read is not the read at entry


def test_a_broken_bias_source_never_stops_the_detector():
    def boom():
        raise OSError("x")
    w = tw.TradeWatch(15, boom)
    step(w, [], now_ms=LATE)
    assert kinds(step(w, [fill(1, LATE, 0)], now_ms=LATE)) == ["LATE_ENTRY"]


def test_one_entry_can_carry_several_events_and_each_fires_once():
    bias = {MES: [{"ts": LATE - 60_000, "grade": "C", "direction": "LONG", "score": 1.0, "adx": 25.0, "flags": []}]}
    w = new(bias)
    base = [fill(1, LATE - 300_000, 0), fill(2, LATE - 200_000, 1, pnl=-50.0)]
    step(w, base, now_ms=LATE - 190_000)
    allf = base + [fill(3, LATE, 1)]
    assert sorted(kinds(step(w, allf, now_ms=LATE))) == ["COUNTER_BIAS", "LATE_ENTRY", "REENTRY"]
    assert step(w, allf, now_ms=LATE + 2000) == []


# ---- output safety -------------------------------------------------------------------------------------------------------

def test_events_carry_only_templates_no_account_money_or_tags():
    w = new({MES: [{"ts": LATE - 60_000, "grade": "evil\n<script>", "direction": "LONG", "score": 1.0, "adx": 1.0, "flags": []}]})
    step(w, [], now_ms=LATE)
    evs = step(w, [fill(1, LATE, 1, pnl=-123.45)], now_ms=LATE)
    blob = repr(w.snapshot(LATE)) + repr(evs)
    assert "4242" not in blob and "123" not in blob and "$" not in blob and "<script>" not in blob
    assert set(e["kind"] for e in evs) <= set(tw.KINDS) and all(re.match(r"^[A-Z_]+:[^:]*:\d+$", e["id"]) for e in evs)


def test_a_hostile_contract_name_becomes_the_word_contract():
    assert tw._word("CON.F.US.<b>.Z26") == "contract" and tw._word(None) == "contract" and tw._word(MES) == "MES"


def test_snapshot_drops_old_events_and_digest_lines_are_fixed_format():
    w = new()
    step(w, [], now_ms=LATE)
    step(w, [fill(1, LATE, 0)], now_ms=LATE)
    assert tw.digest_lines(w.snapshot(LATE))[0] == "TRADEWATCH: OK nostop_s=15 events=1"
    assert any(l.startswith("TRADEWATCH_EVENT: LATE_ENTRY at=15:02CT") for l in tw.digest_lines(w.snapshot(LATE)))
    assert w.snapshot(LATE + tw.SHOW_S * 1000 + 1)["events"] == []
    assert tw.digest_lines(None) == ["TRADEWATCH: OFF"] and tw.digest_lines({"status": "OFF"}) == ["TRADEWATCH: OFF"]


# ---- config --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env,want", [
    ({}, (False, 15.0)), ({"PX_TRADEWATCH": ""}, (False, 15.0)), ({"PX_TRADEWATCH": "0"}, (False, 15.0)), ({"PX_TRADEWATCH": "banana"}, (False, 15.0)),
    ({"PX_TRADEWATCH": "1"}, (True, 15.0)), ({"PX_TRADEWATCH": "1", "PX_TRADEWATCH_NOSTOP_S": "30"}, (True, 30.0)),
    ({"PX_TRADEWATCH_NOSTOP_S": "abc"}, (False, 15.0)), ({"PX_TRADEWATCH_NOSTOP_S": "nan"}, (False, 15.0)),
    ({"PX_TRADEWATCH_NOSTOP_S": "0"}, (False, 3.0)), ({"PX_TRADEWATCH_NOSTOP_S": "1"}, (False, 3.0)), ({"PX_TRADEWATCH_NOSTOP_S": "99999"}, (False, 300.0)),
])
def test_config_defaults_off_and_clamps(env, want):
    assert tw.config(env) == want


# ---- the pin -------------------------------------------------------------------------------------------------------------

def test_the_detector_cannot_reach_the_guard_or_any_write():
    tree = ast.parse((ROOT / "projectx_desk" / "tradewatch.py").read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mods.add(((n.module or "") if not n.level else "." * n.level + (n.module or "")))
            mods.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
    assert not mods & {"guard", "client", "hub", "mcp", "app", "limits", ".guard", ".client", ".hub", ".mcp", ".app", ".limits"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not attrs & {"write", "place", "preview", "flatten", "cancel", "move_stop", "halt"}


def test_the_app_wires_it_default_off_and_the_digest_shows_it():
    app_src = (ROOT / "projectx_desk" / "app.py").read_text()
    assert "px_tradewatch.config()[0]" in app_src and "tradewatch_task" in app_src
    assert "TRADEWATCH" in "\n".join(tw.digest_lines({"status": "OK", "events": [], "nostop_s": 15}))
