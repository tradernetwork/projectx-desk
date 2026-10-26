"""Level-cross detection (static/watch.js, pure, node): baseline silence, hysteresis, direction, cooldown. It says what happened, nothing more."""
import json
import shutil
import subprocess
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

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
