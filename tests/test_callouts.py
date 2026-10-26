"""What happened to a callout since it posted (static/callouts.js, pure, node). Facts only: the entry traded, the stop or a target was touched, how far price is."""
import json
import shutil
import subprocess
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run(c, bars, last, atr=4.0, levels=None):
    js = ("const C=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "const s=C.status(d.c,d.bars,d.last,d.atr,d.levels);console.log(JSON.stringify({s,t:C.text(s)}))")
    out = subprocess.run(["node", "-e", js, str(STATIC / "callouts.js")], input=json.dumps({"c": c, "bars": bars, "last": last, "atr": atr, "levels": levels or []}),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


SHORT = {"side": "SHORT", "entry": 100.0, "stop": 106.0, "tp1": 94.0, "tp2": 88.0, "tp3": 80.0}
B = lambda hi, lo: {"timestamp": 0, "high": hi, "low": lo}


def test_waiting_until_the_entry_trades_then_in_with_distances_signed_to_the_side():
    w = run(SHORT, [B(99, 97)], 98.0)["s"]
    assert w["state"] == "WAITING" and not w["entered"]
    i = run(SHORT, [B(100.5, 98)], 98.0)["s"]
    assert i["state"] == "IN" and i["pts_from_entry"] == 2.0 and i["pts_to_stop"] == 8.0           # short: price below entry is in profit, 8 pts under the stop


def test_targets_are_counted_in_order_and_a_later_stop_is_reported_after_them():
    t2 = run(SHORT, [B(100.5, 99), B(99, 87)], 88.5)
    assert t2["s"]["state"] == "T2" and "T2 touched" in t2["t"]
    after = run(SHORT, [B(100.5, 93), B(107, 100)], 106.0)
    assert after["s"]["state"] == "STOPPED" and after["s"]["targets_hit"] == 1 and "stop touched (after T1)" in after["t"]


def test_a_bar_that_touches_stop_and_target_counts_as_the_stop_and_a_long_mirrors_the_short():
    both = run(SHORT, [B(100.5, 99), B(106.5, 93)], 100.0)["s"]
    assert both["state"] == "STOPPED" and both["targets_hit"] == 0
    long_ = {"side": "LONG", "entry": 100.0, "stop": 96.0, "tp1": 105.0}
    s = run(long_, [B(101, 99.5), B(106, 101)], 105.5)["s"]
    assert s["state"] == "T1" and s["pts_from_entry"] == 5.5


def test_stop_near_a_level_is_flagged_within_a_quarter_atr_and_garbage_gives_nothing():
    n = run(SHORT, [B(100.5, 98)], 98.0, levels=[{"price": 106.5, "roles": ["BRAKE"]}, {"price": 120, "roles": ["CEILING"]}])["s"]
    assert n["stop_near"] == {"price": 106.5, "role": "BRAKE", "atr_dist": 0.13}
    assert run(SHORT, [B(100.5, 98)], 98.0, levels=[{"price": 108.5, "roles": ["BRAKE"]}])["s"]["stop_near"] is None            # 2.5 pts = 0.6 ATR away
    assert run({"side": "SHORT", "entry": None, "stop": 1}, [], 1.0)["s"] is None
    assert run({"side": "FLAT", "entry": 1, "stop": 2}, [], 1.0)["s"] is None


def test_the_row_text_never_rates_the_trade():
    t = run(SHORT, [B(100.5, 98)], 98.0)["t"].lower()
    for w in ("good", "bad", "should", "buy", "sell", "great", "avoid"):
        assert w not in t
