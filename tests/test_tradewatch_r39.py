"""Round 39: STOP_WIDENED and CORRELATED in the trade-event detector. Same silent-baseline and fixed-template rules as the first five."""
from datetime import datetime, timezone

from projectx_desk import tradewatch as tw

MES = "CON.F.US.MES.Z26"
MNQ = "CON.F.US.MNQ.Z26"
GC = "CON.F.US.MGC.Z26"
T0 = int(datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)


def pos(side=1, size=1, cid=MES):
    return {"contractId": cid, "type": side, "size": size, "averagePrice": 7700.0}


def stop_at(price, oid=9, side=0, cid=MES, typ=4, size=1):
    return {"id": oid, "contractId": cid, "type": typ, "side": side, "size": size, "status": 1, "stopPrice": price}


def step(w, positions=(), orders=(), now_ms=T0, seeded=True):
    return w.step([], list(positions), list(orders), seeded, 0.0, now_ms)


def primed(positions, orders):
    w = tw.TradeWatch(15, None)
    step(w, positions, orders)
    return w


def kinds(evs):
    return [e["kind"] for e in evs]


def test_widening_a_long_stop_is_noticed_once_with_a_fixed_template():
    w = primed([pos(1)], [stop_at(7690.0, side=1)])
    ev = step(w, [pos(1)], [stop_at(7680.0, side=1)], T0 + 2000)
    assert kinds(ev) == ["STOP_WIDENED"]
    assert ev[0]["text"] == "MES LONG stop moved away from price by 10 points after it was set"
    assert step(w, [pos(1)], [stop_at(7680.0, side=1)], T0 + 4000) == []      # a new baseline, not told twice


def test_widening_a_short_stop_goes_up_and_tightening_is_silent():
    w = primed([pos(2)], [stop_at(7710.0, side=0)])
    assert step(w, [pos(2)], [stop_at(7700.0, side=0)], T0 + 1000) == []      # toward price: the desk's own tighten
    assert kinds(step(w, [pos(2)], [stop_at(7712.5, side=0)], T0 + 2000)) == ["STOP_WIDENED"]


def test_a_baseline_a_trail_a_replaced_order_and_an_unreadable_trigger_never_fire():
    w = tw.TradeWatch(15, None)
    assert step(w, [pos(1)], [stop_at(7690.0, side=1)]) == []                               # first look is silent
    assert step(w, [pos(1)], [stop_at(7600.0, oid=10, side=1)], T0 + 1000) == []           # new order id: new baseline
    trail = primed([pos(1)], [stop_at(7690.0, oid=3, side=1, typ=5)])
    assert step(trail, [pos(1)], [stop_at(7600.0, oid=3, side=1, typ=5)], T0 + 1000) == []  # a trail moves by design
    for bad in (None, float("nan"), "x", -5, True):
        w2 = primed([pos(1)], [stop_at(7690.0, side=1)])
        assert step(w2, [pos(1)], [stop_at(bad, side=1)], T0 + 1000) == []


def test_a_stop_that_does_not_cover_the_position_is_not_compared():
    w = primed([pos(1, size=2)], [stop_at(7690.0, side=1, size=2)])
    assert step(w, [pos(1, size=2)], [stop_at(7600.0, side=1, size=1)], T0 + 1000) == []   # too small to count as protection


def test_an_unseeded_look_forgets_the_last_trigger_so_a_gap_is_not_a_widening():
    w = primed([pos(1)], [stop_at(7690.0, side=1)])
    step(w, [], [], T0 + 1000, seeded=False)
    assert step(w, [pos(1)], [stop_at(7600.0, side=1)], T0 + 2000) == []


def test_correlated_roots_are_said_once_per_set_and_again_after_they_clear():
    w = primed([pos(1, cid=MES)], [])
    both = [pos(1, cid=MES), pos(2, cid=MNQ)]
    ev = step(w, both, [], T0 + 1000)
    assert kinds(ev) == ["CORRELATED"] and ev[0]["text"] == "MES LONG and MNQ SHORT open at once: both are equity index futures"
    assert step(w, both, [], T0 + 3000) == []
    assert w.snapshot(T0 + 3000)["correlated"] == [{"group": "EQUITY_INDEX", "roots": ["MES", "MNQ"]}]
    step(w, [pos(1, cid=MES)], [], T0 + 5000)
    assert w.snapshot(T0 + 5000)["correlated"] == []
    assert kinds(step(w, both, [], T0 + 7000)) == ["CORRELATED"]


def test_uncorrelated_unknown_or_unreadable_positions_do_not_flag():
    w = primed([], [])
    assert step(w, [pos(1, cid=MES), pos(1, cid=GC)], [], T0 + 1000) == []                  # gold is not in the group
    assert step(w, [pos(1, cid=MES), pos(1, cid=MES)], [], T0 + 2000) == []                 # one root twice is one root
    junk = [pos(1, cid=MES), {"contractId": MNQ, "type": 1, "size": 0}, {"contractId": "junk", "type": 1, "size": 1}, {"contractId": MNQ, "type": 9, "size": 1}]
    assert step(w, junk, [], T0 + 3000) == []
    assert step(w, [pos(1, cid=MES), pos(1, cid=MNQ)], [], T0 + 4000, seeded=False) == []


def test_correlated_at_first_look_is_silent_but_visible_in_the_snapshot_and_digest():
    w = tw.TradeWatch(15, None)
    assert step(w, [pos(1, cid=MES), pos(1, cid=MNQ)]) == []
    snap = w.snapshot(T0)
    assert any(line.startswith("TRADEWATCH_CORR: EQUITY_INDEX roots=MES,MNQ") for line in tw.digest_lines(snap))


def test_the_group_table_is_the_one_definition():
    assert set(tw.CORR_GROUPS["EQUITY_INDEX"]) >= {"MES", "MNQ", "M2K", "MYM"}
    assert "MGC" not in tw.CORR_GROUPS["EQUITY_INDEX"]


def test_the_new_kinds_reach_the_page_and_say_what_happened_never_advice():
    import pathlib
    assert {"STOP_WIDENED", "CORRELATED"} <= set(tw.KINDS)
    js = (pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static" / "watch.js").read_text()
    assert "'STOP_WIDENED'" in js and "'CORRELATED'" in js
    w = primed([pos(1)], [stop_at(7690.0, side=1)])
    texts = [e["text"] for e in step(w, [pos(1), pos(1, cid=MNQ)], [stop_at(7680.0, side=1)], T0 + 1000)]
    assert len(texts) == 2
    for t in texts:
        for word in ("should", "trade now", "reduce", "close", "exit", "flatten", "consider", "avoid", "$", "buy", "sell"):
            assert word not in t.lower(), (t, word)
