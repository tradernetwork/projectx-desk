"""Paper trading journal: when a hypothetical trade opens, how it is graded, and that nothing in it can reach an order."""
import ast
import pathlib
from datetime import datetime

from projectx_desk import paper as P
from projectx_desk.tradeday import CT

CID = "CON.F.US.MES.Z26"
MIN5 = P.BAR_MS


def ms(h, m, day=26, mo=10):
    d = datetime(2026, mo, day, h, 0, tzinfo=CT).timestamp() * 1000
    return int(d + m * 60_000)          # 2026-10-26 is a Monday


def row(h=10, m=0, grade="A", direction="LONG", price=5000.0, atr=4.0, **kw):
    return {"contract": CID, "bar_ts": ms(h, m), "grade": grade, "direction": direction, "price": price, "atr": atr, "score": 3.0, "flags": [], "s21": "UP", "rule": "v2.2", **kw}


def bar(h, m, hi, lo, close=None, day=26):
    return {"timestamp": ms(h, m, day), "high": hi, "low": lo, "close": (hi + lo) / 2 if close is None else close}


def long_trade():
    return P.from_bias(row(), 0.25)           # entry 5000, stop 4994 (1.5 x 4), target 5009 (1.5R)


def test_only_fresh_b_or_better_leans_in_the_entry_windows_open_a_paper_trade():
    t = long_trade()
    assert (t["side"], t["entry"], t["stop"], t["target"], t["stop_dist"]) == ("LONG", 5000.0, 4994.0, 5009.0, 6.0)
    assert t["t"] == ms(10, 5) and t["id"] == f"bias:{CID}:{ms(10, 0)}"
    s = P.from_bias(row(direction="SHORT"), 0.25)
    assert (s["stop"], s["target"]) == (5006.0, 4991.0)
    assert P.from_bias(row(grade="C"), 0.25) is None and P.from_bias(row(direction="NEUTRAL"), 0.25) is None
    assert P.from_bias(row(atr=None), 0.25) is None and P.from_bias(row(), None) is None
    assert P.from_bias(row(), 0.25, open_contracts={CID}) is None                     # one open trade per contract
    assert P.from_bias(row(h=14, m=55), 0.25) is None and P.from_bias(row(h=16, m=30), 0.25) is None        # inside the no-new-entries window
    assert P.from_bias(row(h=19, m=0), 0.25) is not None                               # the evening session is open
    assert P.from_bias(row(h=10, m=0), 0.25) is not None
    assert P.from_bias({**row(), "bar_ts": ms(10, 0, day=23)}, 0.25) is not None       # Friday morning is open
    assert P.from_bias({**row(), "bar_ts": ms(10, 0, day=24)}, 0.25) is None           # Saturday is not
    assert P.market_open(ms(10, 0, day=25)) is False and P.market_open(ms(12, 0, day=25)) is False         # Sunday midday: closed
    assert P.market_open(ms(17, 30, day=25)) is True and P.market_open(ms(17, 30, day=24)) is False        # Sunday reopen yes, Saturday no


def test_target_stop_and_the_both_in_one_bar_rule():
    t = long_trade()
    now = ms(12, 0)
    win = P.resolve(t, [bar(10, 5, 5003, 4999), bar(10, 10, 5010, 5002)], now)
    assert (win["result"], win["r"], win["bars"]) == ("TARGET", 1.5, 2)
    loss = P.resolve(t, [bar(10, 5, 5003, 4993)], now)
    assert (loss["result"], loss["r"]) == ("STOP", -1.0)
    both = P.resolve(t, [bar(10, 5, 5010, 4993)], now)
    assert both["result"] == "STOP"                                                      # a bar that touches both is the stop
    assert P.resolve(t, [bar(10, 5, 5003, 4999)], now) is None                           # still open


def test_the_forming_bar_is_ignored_and_bars_before_the_trade_do_not_count():
    t = long_trade()
    forming = bar(10, 5, 5010, 4990)
    assert P.resolve(t, [forming], ms(10, 7)) is None                                    # not closed yet: no outcome from half a bar
    assert P.resolve(t, [bar(10, 0, 5020, 4980)], ms(12, 0)) is None                     # the bar the signal fired on is not part of the trade


def test_the_deadline_and_the_horizon_close_a_trade_at_the_bar_close_in_r():
    t = P.from_bias(row(h=14, m=40), 0.25)
    bars = [bar(14, 45, 5002, 4998, 5003 - 1), bar(14, 50, 5003, 4999, 5002), bar(14, 55, 5004, 5000, 5003), bar(15, 0, 5005, 5001, 5004), bar(15, 5, 5008, 5004, 5007)]
    r = P.resolve(t, bars, ms(15, 30))
    assert r["result"] == "FLAT_BY" and r["exit"] == 5004 and r["r"] == round((5004 - 5000) / 6.0, 2)        # the 15:05 bar runs to 15:10, past the 15:08 deadline
    t2 = long_trade()
    drift = [bar(10, 5 + 5 * i, 5004, 5001, 5002) for i in range(P.HORIZON_BARS)]          # ms() carries minutes past 59
    h = P.resolve(t2, drift, ms(14, 0))
    assert h["result"] == "TIME" and h["bars"] == P.HORIZON_BARS and h["r"] == round(2 / 6.0, 2)


def test_the_opposite_side_baseline_is_graded_on_the_same_bars():
    t = long_trade()
    r = P.resolve(t, [bar(10, 5, 5010, 5002)], ms(12, 0))
    assert r["result"] == "TARGET" and r["r_opp"] == -1.0                                # the same bar stops the short at 5006
    r2 = P.resolve(t, [bar(10, 5, 5003, 4993)], ms(12, 0))
    assert r2["result"] == "STOP" and r2["r_opp"] == round(-(4998 - 5000) / 6.0, 2)       # the short is undecided when the long stops: marked at that bar's close (4998)


def callout(age=120, **kw):
    return {"event": "ENTRY", "symbol": "NQ", "side": "SHORT", "entry": 5000.0, "stop": 5008.0, "tp1": 4984.0, "age_s": age, "analyst": "@someone", **kw}


def test_a_callout_trade_waits_for_its_entry_and_a_garbled_post_opens_nothing():
    now = ms(10, 10)
    t = P.from_callout(callout(), CID, 0.25, now)
    assert (t["side"], t["entry"], t["stop"], t["target"], t["stop_dist"], t["wait_entry"]) == ("SHORT", 5000.0, 5008.0, 4984.0, 8.0, True)
    assert P.from_callout(callout(stop=4990.0), CID, 0.25, now) is None                 # a short whose stop is below entry
    assert P.from_callout(callout(tp1=5010.0), CID, 0.25, now) is None                  # a short whose target is above entry
    assert P.from_callout(callout(event="TRIM"), CID, 0.25, now) is None and P.from_callout(callout(entry=None), CID, 0.25, now) is None
    assert P.from_callout(callout(side=None), CID, 0.25, now)["side"] == "SHORT"        # side inferred from the stop
    assert P.from_callout(callout(), CID, 0.25, now, open_contracts={CID}) is None
    done = P.resolve(t, [bar(10, 10, 4999, 4980)], ms(10, 40))
    assert done is None or done["result"] != "MISSED"
    hit = P.resolve(t, [bar(10, 5, 5009, 4970), bar(10, 10, 5001, 4995), bar(10, 15, 4999, 4983)], ms(11, 0))          # the 10:05 bar is before the post (10:08): ignored
    assert hit["result"] == "TARGET" and hit["r"] == 2.0 and hit["r_opp"] is None       # a callout has no baseline of its own
    never = P.resolve(t, [bar(10, 10 + 5 * i, 4990, 4985) for i in range(P.CALLOUT_WAIT_BARS)], ms(12, 0))
    assert never["result"] == "MISSED" and never["r"] is None                           # price never traded up to the entry


def test_open_new_is_idempotent_fresh_only_and_one_per_contract_and_close_due_grades_them():
    now = ms(10, 6)
    bias = [row(), row(h=9, m=0, grade="B"), row(h=10, m=0, direction="SHORT")]         # the 09:00 one is stale, the second 10:00 one is the same contract
    new = P.open_new([], bias, [], {"MES": CID}.get, lambda c: 0.25, now)
    assert [r["id"] for r in new] == [f"bias:{CID}:{ms(10, 0)}"] and new[0]["kind_row"] == "open"
    j = list(new)
    assert P.open_new(j, bias, [], {"MES": CID}.get, lambda c: 0.25, now) == []        # nothing twice
    assert P.close_due(j, lambda c: [bar(10, 5, 5003, 4999)], ms(10, 6)) == []         # the 10:05 bar is still forming
    closes = P.close_due(j, lambda c: [bar(10, 5, 5010, 5002)], ms(10, 12))
    assert closes and closes[0]["kind_row"] == "close" and closes[0]["result"] == "TARGET" and closes[0]["grade"] == "A"
    j += closes
    assert P.state(j)[0] == []                                                           # closed: no longer open
    again = P.open_new(j, [row(h=10, m=5, grade="B", price=5010.0)], [], {"MES": CID}.get, lambda c: 0.25, ms(10, 11))
    assert len(again) == 1                                                               # the contract is free again
    nq = P.open_new(j, [], [callout()], {"MNQ": "CON.F.US.MNQ.Z26"}.get, lambda c: 0.25, ms(10, 10))
    assert nq and nq[0]["contract"] == "CON.F.US.MNQ.Z26" and nq[0]["source"] == "callout"        # NQ is traded as MNQ


def test_stats_withhold_a_rate_under_thirty_and_never_average_missed_callouts():
    few = [{"result": "TARGET", "r": 1.5, "r_opp": -1.0}, {"result": "STOP", "r": -1.0, "r_opp": 1.0}, {"result": "MISSED", "r": None}]
    s = P.stats(few)
    assert s["n"] == 2 and s["missed"] == 1 and s["win_pct"] is None and s["mean_r"] is None and s["total_r"] == 0.5 and not s["enough"]
    many = [{"result": "TARGET", "r": 1.5, "r_opp": -1.0}] * 20 + [{"result": "STOP", "r": -1.0, "r_opp": 1.0}] * 20
    s = P.stats(many)
    assert s["enough"] and s["win_pct"] == 50.0 and s["mean_r"] == 0.25 and s["mean_r_opp"] == 0.0
    text = P.report([{"kind_row": "open", "id": "x", "source": "bias", "contract": CID, "side": "LONG", "entry": 5000.0, "stop": 4994.0, "target": 5009.0}]
                    + [{"kind_row": "close", "id": f"c{i}", "source": "bias", "contract": CID, "side": "LONG", "grade": "A", "result": "TARGET", "r": 1.5, "r_opp": -1.0} for i in range(3)])
    assert "3 closed, 1 open" in text and "no rate" in text and "OPEN bias MES LONG" in text


def test_the_paper_journal_cannot_reach_an_order_path():
    for path in ("projectx_desk/paper.py", "scripts/paper_journal.py"):
        src = (pathlib.Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not names & {"write", "place", "preview", "flatten", "stage", "PXClient", "Guard"}, (path, names & {"write", "place", "preview", "flatten", "stage"})
        assert not any(m.endswith(("guard", "client", "hub")) or m in ("projectx_desk.guard", "projectx_desk.client", "projectx_desk.hub") for m in mods), (path, mods)
        assert "/api/place" not in src and "/api/preview" not in src
