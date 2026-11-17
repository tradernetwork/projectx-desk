"""Notifier: event diffing from synthetic state dicts, dedupe, rate limit, boot grace, and what a payload may ever contain."""
import json
import re

import pytest

from projectx_desk import notify
from projectx_desk.notify import Detector, Limiter, Notifier

MES = "CON.F.US.MES.Z26"
MNQ = "CON.F.US.MNQ.Z26"


def state(**o):
    s = {"mode": {"halt": False, "unknown_order": False}, "feed": {"market": "UP", "user": "UP", "seeded": True},
         "risk": {"daily_limit": 1000.0, "daily_room": 900.0, "dd_buffer": 2000.0}, "positions": [], "orders": [],
         "trade": None, "callouts": {"status": "OFF", "items": []}, "regime": {"state": "UNKNOWN"}}
    for k, v in o.items():
        s[k] = {**s[k], **v} if isinstance(v, dict) and isinstance(s.get(k), dict) else v
    return s


def pos(cid=MES, side="LONG", size=1):
    return {"contract": cid, "side": side, "size": size, "avg_price": 5000.0}


def trade(i, cid=MES, order=1, side=1, pnl=-50.0, fees=1.0):
    return {"id": i, "contractId": cid, "orderId": order, "side": side, "profitAndLoss": pnl, "fees": fees,
            "accountId": 424242, "price": 1.0}


def kinds(evs):
    return [(e.kind, e.text) for e in evs]


def run(d, cur, extras=None, now=0.0):
    return d.step(cur, extras or {}, now)


# ---- diffing ----------------------------------------------------------------------------------------------------------

def test_halt_fires_once_on_the_edge_and_again_after_a_clear():
    d = Detector()
    assert run(d, state()) == []
    assert kinds(run(d, state(mode={"halt": True}))) == [("halt", "HALT: new orders refused")]
    assert run(d, state(mode={"halt": True})) == []
    run(d, state())
    assert len(run(d, state(mode={"halt": True}))) == 1


def test_unreconciled_order_is_reported_as_a_halt_class_event():
    d = Detector()
    run(d, state())
    ev = run(d, state(mode={"unknown_order": True}))
    assert kinds(ev) == [("halt", "Unreconciled order: check desk")] and ev[0].critical


def test_position_going_flat_and_stop_hit_are_told_apart_and_not_double_reported():
    d = Detector()
    stop = {5: {"id": 5, "type": 4, "status": 1, "contractId": MES}}
    run(d, state(positions=[pos()]), {"orders": stop, "trades": []})
    ev = run(d, state(positions=[]), {"orders": {}, "trades": [trade(9, order=5)]})
    assert kinds(ev) == [("stop", "Stop hit MES")]                       # not also a fill and a flat
    d = Detector()
    run(d, state(positions=[pos()]), {"orders": stop, "trades": []})
    ev = run(d, state(positions=[]), {"orders": {}, "trades": [trade(9, order=77)]})
    assert kinds(ev) == [("flat", "Flat MES")]                           # closed by something that was not our stop


def test_fill_event_on_a_new_trade_only_and_never_for_the_seeded_ones():
    d = Detector()
    run(d, state(), {"trades": []})                                      # primes the trade ids
    ev = run(d, state(positions=[pos()]), {"trades": [trade(1, side=0, pnl=None)]})
    assert kinds(ev) == [("fill", "Fill MES BUY")]
    assert run(d, state(positions=[pos()]), {"trades": [trade(1, side=0, pnl=None)]}) == []     # replayed fill is not new


def test_stop_failure_and_unprotected_position_are_critical():
    d = Detector()
    run(d, state(positions=[pos()]))
    ev = run(d, state(positions=[pos()], trade={"stop_state": "FAILED"}))
    assert kinds(ev) == [("stop", "Stop FAILED MES: check desk")] and ev[0].critical
    assert run(d, state(positions=[pos()], trade={"stop_state": "FAILED"})) == []
    ev = run(d, state(positions=[pos(MNQ)], trade={"stop_state": "FAILED"}), {"protection": {MNQ: "EXTERNAL_UNPROTECTED", MES: "EXTERNAL_UNPROTECTED"}})
    assert ("stop", "Unprotected position MNQ") in kinds(ev) and all(e.critical for e in ev if e.key.startswith("unprotected"))
    assert all("MES" not in e.text for e in ev if e.kind == "stop" and "Unprotected" in e.text)   # MES has no open position


def test_room_below_25_percent_fires_on_the_edge_and_rearms_with_hysteresis():
    d = Detector()
    assert run(d, state()) == []
    assert kinds(run(d, state(risk={"daily_room": 240.0}))) == [("room", "Daily room low")]
    assert run(d, state(risk={"daily_room": 100.0})) == []
    assert run(d, state(risk={"daily_room": 300.0})) == []               # between 25% and 35%: still disarmed
    run(d, state(risk={"daily_room": 400.0}))
    assert kinds(run(d, state(risk={"daily_room": 100.0}))) == [("room", "Daily room low")]


def test_drawdown_room_uses_the_limit_when_known_else_the_best_seen():
    d = Detector()
    ev = run(d, state(risk={"dd_buffer": 400.0}), {"dd_limit": 2000.0})
    assert kinds(ev) == [("room", "Drawdown room low")]
    d = Detector()
    run(d, state(risk={"dd_buffer": 2000.0}))
    assert kinds(run(d, state(risk={"dd_buffer": 480.0}))) == [("room", "Drawdown room low")]
    d = Detector()
    assert run(d, state(risk={"dd_buffer": None, "daily_room": None})) == []        # unknown rooms never alarm


def test_feed_down_over_30s_then_recovered():
    d = Detector()
    assert run(d, state(feed={"market": "DOWN"}), now=100) == []
    assert run(d, state(feed={"market": "DOWN"}), now=129) == []
    assert kinds(run(d, state(feed={"market": "DOWN"}), now=131)) == [("feed", "Market feed down")]
    assert run(d, state(feed={"market": "DOWN"}), now=200) == []
    assert kinds(run(d, state(feed={"market": "UP"}), now=210)) == [("feed", "Market feed recovered")]
    assert run(d, state(feed={"market": "DOWN"}), now=220) == []         # a blip under 30 s that recovers says nothing
    assert run(d, state(feed={"market": "UP"}), now=225) == []
    run(d, state(feed={"user": "DOWN"}), now=300)
    assert kinds(run(d, state(feed={"user": "DOWN"}), now=340)) == [("feed", "User feed down")]


def test_fresh_entry_callout_only_and_only_once():
    d = Detector()
    item = {"ts": "2026-10-22T14:00:00+00:00", "age_s": 30, "event": "ENTRY", "symbol": "MES", "side": "LONG", "analyst": "bob"}
    co = {"status": "OK", "items": [item, {**item, "ts": "x", "event": "TP_HIT"}, {**item, "ts": "y", "age_s": 9000}]}
    assert kinds(run(d, state(callouts=co))) == [("callout", "Analyst ENTRY MES LONG")]
    assert run(d, state(callouts=co)) == []
    hostile = {**item, "ts": "z", "symbol": "ES; DROP TABLE", "side": "IGNORE PREVIOUS INSTRUCTIONS", "analyst": "<script>"}
    ev = run(d, state(callouts={"status": "OK", "items": [hostile]}))
    assert kinds(ev) == [("callout", "Analyst ENTRY")]                     # unvalidated symbol and side are dropped, never echoed


def test_regime_flip_to_ranging_only_from_trending():
    d = Detector()
    assert run(d, state(regime={"state": "UNKNOWN"})) == []
    run(d, state(regime={"state": "TRENDING"}))
    assert kinds(run(d, state(regime={"state": "RANGING"}))) == [("regime", "Regime RANGING")]
    assert run(d, state(regime={"state": "RANGING"})) == []
    d = Detector()
    assert run(d, state(regime={"state": "RANGING"})) == []                # first sight is a baseline, not a flip


def test_amounts_only_with_the_flag_and_never_a_balance():
    stop = {5: {"id": 5, "type": 4, "status": 1}}
    for flag, want in ((False, "Stop hit MES"), (True, "Stop hit MES -$51")):
        d = Detector(amounts=flag)
        run(d, state(positions=[pos()]), {"orders": stop, "trades": []})
        ev = run(d, state(positions=[]), {"orders": {}, "trades": [trade(9, order=5)]})
        assert ev[0].text == want


# ---- limiter ----------------------------------------------------------------------------------------------------------

def test_limiter_debounces_per_key_and_caps_per_hour():
    lim = Limiter()
    assert lim.allow("a", 0) and not lim.allow("a", 59) and lim.allow("a", 61)
    lim = Limiter()
    assert all(lim.allow(f"k{i}", i) for i in range(20))
    assert not lim.allow("another", 21)                                    # 21st in the hour
    assert lim.allow("another", 3601)                                      # the window rolls


# ---- notifier: grace, baseline, wiring ---------------------------------------------------------------------------------

class FakePusher:
    def __init__(self, prefs=None):
        from projectx_desk import push
        self.store = type("S", (), {"subs": {"a" * 16: {"prefs": push.clean_prefs(prefs or {})}}})()
        self.sent = []

    async def broadcast(self, kind, payload, critical=False):
        self.sent.append((kind, payload, critical))
        return 1


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(prefs=None, states=None, extras=None, grace=10.0):
    clk, fp = Clock(), FakePusher(prefs)
    seq = states if states is not None else []

    async def state_fn(c):
        return seq[0] if len(seq) == 1 else seq.pop(0)

    n = Notifier(fp, state_fn, (lambda: extras) if extras is not None else None, lambda: MES, grace_s=grace, clock=clk)
    return n, fp, clk, seq


async def test_boot_grace_and_baseline_swallow_state_that_already_exists():
    n, fp, clk, _ = make(states=[state(mode={"halt": True}, positions=[pos()])], extras={"trades": [trade(1)], "orders": {}})
    for _ in range(4):
        await n.tick()
        clk.t += 5
    assert fp.sent == []                                                   # already halted and already in a position at boot: not news
    n.det.halt = False                                                     # ...but a genuine new halt later does fire
    clk.t += 5
    await n.tick()
    assert [s[0] for s in fp.sent] == ["halt"]


async def test_nothing_but_feed_and_halt_before_the_desk_is_seeded():
    unseeded = state(feed={"seeded": False}, positions=[pos()])
    n, fp, clk, seq = make(states=[unseeded, unseeded, state(feed={"seeded": False, "user": "DOWN"})], grace=0.0)
    await n.tick(); await n.tick()
    clk.t += 45
    await n.tick()
    assert fp.sent == []                                                   # user DOWN just started


async def test_feed_outage_alerts_even_while_unseeded_and_recovery_follows():
    down = state(feed={"seeded": False, "user": "DOWN"})
    n, fp, clk, seq = make(states=[state(), down], grace=0.0)
    await n.tick()
    clk.t += 1
    await n.tick()
    clk.t += 40
    seq.append(down)
    await n.tick()
    assert [(k, p["body"]) for k, p, _ in fp.sent] == [("feed", "User feed down")]


async def test_per_subscription_toggle_gates_the_event_and_rate_limit_applies():
    n, fp, clk, seq = make(prefs={"events": {"halt": False, "room": True}}, states=[state(), state(mode={"halt": True}), state(mode={"halt": True}, risk={"daily_room": 10.0})], grace=0.0)
    for _ in range(3):
        await n.tick()
        clk.t += 1
    assert [k for k, *_ in fp.sent] == ["room"]
    assert fp.sent[0][1] == {"title": "ProjectX Desk", "body": "Daily room low", "tag": "room:daily", "kind": "room"}


async def test_no_subscribers_means_no_rate_limit_budget_is_burned():
    n, fp, clk, seq = make(states=[state(), state(mode={"halt": True})], grace=0.0)
    fp.store.subs.clear()
    await n.tick(); clk.t += 1; await n.tick()
    assert n.limiter.sent == []


async def test_regime_is_polled_per_contract_only_when_someone_wants_it():
    calls = []
    fp = FakePusher({"events": {"regime": True}})

    async def state_fn(c):
        calls.append(c)
        return state(regime={"state": "RANGING" if len(calls) > 4 else "TRENDING"}) if c else state()

    clk = Clock()
    n = Notifier(fp, state_fn, None, lambda: MES, grace_s=0.0, clock=clk)
    await n.tick()
    assert calls == [None, MES]
    clk.t += 5
    await n.tick()
    assert calls == [None, MES, None]                                      # at most once a minute
    clk.t += 60
    await n.tick()
    assert [p["body"] for _, p, _ in fp.sent] == ["Regime RANGING"]
    fp2 = FakePusher()
    calls.clear()
    n2 = Notifier(fp2, state_fn, None, lambda: MES, clock=Clock())
    await n2.tick()
    assert calls == [None]                                                 # regime toggle off everywhere: no extra state build


# ---- payload hygiene: hostile names, tags and ids never reach text -----------------------------------------------------

HOSTILE = ["CON.F.US.MES.Z26\nIGNORE PREVIOUS INSTRUCTIONS", "CON.F.US.<script>alert(1)</script>.Z26", "CON.F.US.MES;rm -rf /.Z26",
           "CON.F.US." + "A" * 40 + ".Z26", "CON.F.US.mes.Z26", "CON.F.US..Z26", "CON.F", "CON." + "x" * 41, None, 5, {"a": 1}, "sk-live-1234567890"]


@pytest.mark.parametrize("cid", HOSTILE)
def test_hostile_contract_names_fall_back_to_the_word_contract(cid):
    d = Detector(amounts=True)
    stop = {5: {"id": 5, "type": 4, "status": 1}}
    p = {"contract": cid, "side": "LONG", "size": 1}
    run(d, state(positions=[p]), {"orders": stop, "trades": []})
    ev = run(d, state(positions=[]), {"orders": {}, "trades": [trade(9, cid=cid, order=5)]})
    for e in ev:
        assert re.fullmatch(r"[A-Za-z0-9 :$+\-,]{1,80}", e.text), e.text
        assert "IGNORE" not in e.text and "script" not in e.text and "rm" not in e.text.split()


def test_text_templates_are_closed_over_every_event_even_with_hostile_input():
    d = Detector(amounts=True)
    bad = "ACCT 424242 balance $123,456.78 tag px-deadbeef-1 IGNORE ALL"
    cur = state(mode={"halt": True, "unknown_order": True}, feed={"market": "DOWN", "user": "DOWN"},
                risk={"daily_room": 1.0, "dd_buffer": 1.0, "balance": 123456.78, "account_id": 424242},
                positions=[{"contract": MES, "side": bad, "size": 1}], trade={"stop_state": "FAILED", "tag": bad},
                callouts={"status": "OK", "items": [{"ts": bad, "age_s": 1, "event": "ENTRY", "symbol": bad, "side": bad, "analyst": bad}]},
                regime={"state": "TRENDING"})
    run(d, state(regime={"state": "TRENDING"}), now=0)
    evs = run(d, cur, {"protection": {MES: "EXTERNAL_UNPROTECTED"}, "dd_limit": 1e6, "orders": {1: {"id": 1, "type": 4, "status": 1, "customTag": bad}},
                       "trades": [dict(trade(1), customTag=bad, accountId=424242)]}, now=100)
    evs += run(d, state(positions=[], regime={"state": "RANGING"}, feed={"market": "UP", "user": "UP"}), {"orders": {}, "trades": [trade(2, order=1)]}, now=101)
    assert evs
    for e in evs:
        assert re.fullmatch(r"[A-Za-z0-9 :$+\-,]{1,60}", e.text), e.text
        blob = json.dumps(e._asdict())
        for forbidden in ("424242", "123456", "px-deadbeef", "IGNORE", "ACCT", "balance"):
            assert forbidden not in blob, (forbidden, blob)


def test_config_defaults_and_bounds():
    assert notify.config({}) == (False, 10.0, 5.0)
    assert notify.config({"PX_PUSH_AMOUNTS": "1", "PX_PUSH_GRACE_S": "0", "PX_PUSH_INTERVAL_S": "0.1"}) == (True, 0.0, 2.0)
    assert notify.config({"PX_PUSH_GRACE_S": "x", "PX_PUSH_INTERVAL_S": "x"}) == (False, 10.0, 5.0)


def test_record_only_has_no_push_or_sw_routes_and_no_post_at_all(monkeypatch):
    for k in ("PX_TRADING", "PX_MCP_PLACE", "PX_RECORD_ONLY", "PX_RECORDER_URL", "PX_MCP_TOKEN", "PX_OPERATOR_TOKEN", "PX_HOST"):
        monkeypatch.delenv(k, raising=False)
    from projectx_desk.app import create_app
    from projectx_desk.hub import SignalRHub
    from test_px_recorder_mode import FakeClient
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"), allowed_hosts={"testserver"},
                     connect=False, record_only=True)
    paths = {r.path for r in app.routes}
    assert not [p for p in paths if p.startswith("/api/push") or p in ("/sw.js", "/static")]
    for r in app.routes:
        assert set(getattr(r, "methods", ()) or ()) <= {"GET", "HEAD"}


# ---- hysteresis, per-contract cap, held-back summary (Task A) ---------------------------------------------------------

from projectx_desk.notify import LevelWatch


def test_per_contract_cap_is_five_per_hour_and_held_alerts_do_not_consume_the_window():
    lim = Limiter()
    assert [lim.allow(f"fill:MES:{i}", i, "MES") for i in range(7)] == [True] * 5 + [False] * 2
    assert lim.held == 2 and len(lim.sent) == 5                            # held ones are counted, not charged to the window
    assert lim.allow("fill:MNQ", 8, "MNQ")                                 # another contract is unaffected


def test_an_alert_exactly_one_hour_old_is_outside_the_window():
    lim = Limiter()
    assert all(lim.allow(f"k{i}", 0) for i in range(20))
    assert not lim.allow("x", 3599.9)
    assert lim.allow("y", 3600.0)                                          # exactly 3600 s old: out of the window
    lim = Limiter()
    assert all(lim.allow(f"m{i}", 0, "MES") for i in range(5))
    assert not lim.allow("m5", 3599, "MES") and lim.allow("m6", 3600, "MES")


def test_summary_is_due_once_when_the_window_frees_and_never_before():
    lim = Limiter()
    for i in range(5):
        lim.allow(f"a{i}", i, "MES")
    lim.allow("b", 10, "MES"); lim.allow("c", 11, "MES"); lim.allow("d", 12, "MES")
    assert lim.held == 3
    assert lim.take_held(100) is None                                      # window still full for MES
    assert lim.take_held(3600.0 + 4) == 3                                  # the first five aged out
    assert lim.take_held(3600.0 + 5) is None and lim.held == 0             # once only


def test_global_cap_holds_are_counted_too():
    lim = Limiter(cap=2)
    lim.allow("a", 0); lim.allow("b", 1)
    assert not lim.allow("c", 2) and lim.held == 1
    assert lim.take_held(3601.0) == 1


async def test_notifier_sends_one_fixed_summary_and_logs_every_event_even_when_held():
    states = [state()] + [state(positions=[pos()]) if i % 2 == 0 else state() for i in range(1, 15)]
    seq = list(states)

    async def state_fn(c):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    clk, fp = Clock(), FakePusher({"events": {"flat": True, "fill": True, "stop": True}})
    trades, n = [], None
    extras = lambda: {"orders": {}, "trades": list(trades)}                 # noqa: E731
    n = Notifier(fp, state_fn, extras, lambda: MES, grace_s=0.0, clock=clk, limiter=Limiter(debounce=0.0))
    for i in range(14):
        if i % 2 == 1:
            trades.append(trade(i, order=99))
        await n.tick()
        clk.t += 1
    bodies = [p["body"] for _, p, _ in fp.sent]
    assert len([b for b in bodies if b.startswith("Flat MES")]) <= 5       # per-contract cap
    logged = [e for e in n.log if e.text.startswith("Flat MES")]
    assert len(logged) > 5                                                  # logged even when not pushed
    clk.t += 3600
    seq[:] = [state()]
    await n.tick()
    summ = [p["body"] for _, p, _ in fp.sent if "held back" in p["body"]]
    assert summ and re.fullmatch(r"\d+ alerts held back", summ[0])
    clk.t += 5
    await n.tick()
    assert len([1 for _, p, _ in fp.sent if "held back" in p["body"]]) == 1


# five M17 cases for a level watch

def lw():
    return LevelWatch(hyst=0.0, rearm=0.0)


def test_m17_first_observation_records_the_side_and_never_fires():
    w = lw()
    assert w.observe({"apex": 100.0}, 101.0, 1.0, 0) == []
    assert w.observe({"apex": 100.0}, 101.5, 1.0, 1) == []


def test_m17_a_gap_through_a_level_fires_exactly_once():
    w = lw()
    w.observe({"apex": 100.0}, 98.0, 1.0, 0)
    ev = w.observe({"apex": 100.0}, 103.0, 1.0, 1)
    assert [(e.kind, e.text) for e in ev] == [("level", "Level crossed up")]
    assert w.observe({"apex": 100.0}, 104.0, 1.0, 2) == []


def test_m17_touching_without_crossing_does_not_fire():
    w = lw()
    w.observe({"apex": 100.0}, 99.0, 1.0, 0)
    assert w.observe({"apex": 100.0}, 100.0, 1.0, 1) == []
    assert w.observe({"apex": 100.0}, 99.5, 1.0, 2) == []


def test_m17_a_vanished_level_snoozes_with_exactly_one_event_never_a_fire():
    w = lw()
    w.observe({"apex": 100.0}, 99.0, 1.0, 0)
    ev = w.observe({}, 103.0, 1.0, 1)
    assert [(e.kind, e.text) for e in ev] == [("level", "Level gone")]
    assert w.observe({}, 104.0, 1.0, 2) == []                               # exactly one
    assert w.observe({"apex": 100.0}, 99.0, 1.0, 3) == []                  # it comes back as a fresh silent baseline


def test_m17_a_stale_quote_skips_and_records_nothing():
    w = lw()
    w.observe({"apex": 100.0}, 99.0, 1.0, 0)
    assert w.observe({"apex": 100.0}, 103.0, 25.0, 1) == []                 # older than 20 s
    assert w.observe({"apex": 100.0}, 103.0, None, 2) == []
    assert [e.text for e in w.observe({"apex": 100.0}, 103.0, 20.0, 3)] == ["Level crossed up"]   # the side was never overwritten


def test_levelwatch_rearm_needs_a_retreat_and_text_is_a_closed_template():
    w = LevelWatch(hyst=0.25, rearm=1.0)
    w.observe({"a": 100.0}, 99.0, 1.0, 0)
    assert [e.text for e in w.observe({"a": 100.0}, 101.0, 1.0, 1)] == ["Level crossed up"]
    w.observe({"a": 100.0}, 99.5, 1.0, 2)
    assert w.observe({"a": 100.0}, 101.0, 1.0, 3) == []                     # only 0.5 retreat
    w.observe({"a": 100.0}, 98.9, 1.0, 4)
    assert [e.text for e in w.observe({"a": 100.0}, 101.0, 1.0, 5)] == ["Level crossed up"]
