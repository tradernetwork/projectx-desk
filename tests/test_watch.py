"""Level-cross detection (static/watch.js, pure, node): baseline silence, hysteresis, direction, cooldown. It says what happened, nothing more."""
import json
import shutil
import subprocess
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

JSR = """const W=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));
const sides=new Map(), fired=new Map(), armed=new Map(), out=[];
for(const [t,p] of d.steps){ out.push(W.crossEvents(d.items,p,d.h,sides,fired,t,d.cool,null,{rearm:d.rearm,armed}).map(e=>[e.role,e.side])) }
console.log(JSON.stringify(out))"""

JS = """const W=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));
const sides=new Map(), fired=new Map(), out=[];
for(const [t,p] of d.steps){ out.push(W.crossEvents(d.items,p,d.h,sides,fired,t,d.cool,d.only?(r=>d.only.includes(r)):null).map(e=>[e.role,e.side])) }
console.log(JSON.stringify(out))"""


def run(steps, items=None, h=0.25, cool=120_000, only=None):
    items = items if items is not None else [{"price": 100.0, "roles": ["TRAPDOOR"]}]
    out = subprocess.run(["node", "-e", JS, str(STATIC / "watch.js")], input=json.dumps({"steps": steps, "items": items, "h": h, "cool": cool, "only": only}),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_loading_the_page_below_a_level_is_silent_and_the_first_cross_down_fires_once():
    r = run([(0, 101.0), (1000, 100.6), (2000, 99.5), (3000, 99.2)])
    assert r == [[], [], [["TRAPDOOR", -1]], []]                       # a baseline above, one cross down, then nothing more while it stays below


def test_a_wiggle_inside_the_hysteresis_band_is_not_a_cross():
    assert run([(0, 100.5), (1000, 100.1), (2000, 99.9), (3000, 100.2), (4000, 100.6)]) == [[], [], [], [], []]


def test_a_reclaim_is_announced_as_an_up_cross_and_the_same_direction_waits_out_its_cooldown():
    r = run([(0, 101.0), (1000, 99.0), (2000, 101.0), (3000, 99.0), (200_000, 101.0), (201_000, 99.0)], cool=120_000)
    assert [x for x in r] == [[], [["TRAPDOOR", -1]], [["TRAPDOOR", 1]], [], [["TRAPDOOR", 1]], [["TRAPDOOR", -1]]]


def test_roles_can_be_filtered_and_junk_is_ignored():
    items = [{"price": 100.0, "roles": ["PIN", "TRAPDOOR"]}, {"price": None, "roles": ["FLIP"]}, {"price": 105.0}]
    assert run([(0, 101.0), (1000, 99.0)], items=items, only=["TRAPDOOR"]) == [[], [["TRAPDOOR", -1]]]
    assert run([(0, 101.0), (1000, None), (2000, float("nan") if False else 99.0)], items=items, only=["TRAPDOOR"])[1] == []


def run_rearm(steps, rearm, h=0.25, cool=0):
    items = [{"price": 100.0, "roles": ["TRAPDOOR"]}]
    out = subprocess.run(["node", "-e", JSR, str(STATIC / "watch.js")], input=json.dumps({"steps": steps, "items": items, "h": h, "cool": cool, "rearm": rearm}),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_rearm_needs_price_to_retreat_before_the_same_level_and_direction_fires_again():
    # fires up at 101; dips to 99.5 (only 0.5 past the level, rearm needs 1.0); the up cross at 101 must stay silent (the first down cross at 99.5 is not rearm-gated)
    r = run_rearm([(0, 99.0), (1, 101.0), (2, 99.5), (3, 101.0), (4, 98.9), (5, 101.0)], rearm=1.0)
    assert r == [[], [["TRAPDOOR", 1]], [["TRAPDOOR", -1]], [], [["TRAPDOOR", -1]], [["TRAPDOOR", 1]]]


def test_without_rearm_options_behaviour_is_unchanged():
    r = run([(0, 99.0), (1, 101.0), (2, 99.5), (3, 101.0)], cool=0)
    assert r == [[], [["TRAPDOOR", 1]], [["TRAPDOOR", -1]], [["TRAPDOOR", 1]]]
