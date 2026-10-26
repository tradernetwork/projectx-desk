"""Dealer-map words and level logic (static/tmap.js, pure, node): merged strikes, the right-now line, and colours that never call a breakdown point 'go'."""
import json
import shutil
import subprocess
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def js(expr, payload=None):
    code = f"const T=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code, str(STATIC / "tmap.js")], input=json.dumps(payload or {}), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


MAP = {"typical_move": 20.0, "roads": [
    {"key": "UP", "waypoints": [{"price": 5050.0, "tag": "BRAKE"}, {"price": 5010.0, "tag": "ACCELERANT"}]},
    {"key": "DOWN", "waypoints": [{"price": 4950.0, "tag": "TRAPDOOR"}, {"price": 5010.0, "tag": "PIN"}, {"price": 4900.0, "tag": "LAST SHELF"}]}]}


def test_levels_merge_roles_on_one_strike_and_sort_top_to_bottom():
    lv = js("T.tmapLevels(d)", MAP)
    assert [x["price"] for x in lv] == [5050.0, 5010.0, 4950.0, 4900.0]
    assert sorted(lv[1]["roles"]) == ["ACCELERANT", "PIN"] and lv[1]["road"] == "UP"      # the first road to name a strike keeps it


def test_now_line_says_at_when_within_tolerance_and_otherwise_names_the_nearest_each_side():
    at = js("T.nowText(d.m, d.p, false)", {"m": MAP, "p": 4950.5})
    assert at.startswith("Price 4950.5 is AT the 4950 TRAPDOOR") and "not a signal" in at and "buy" not in at.lower()
    between = js("T.nowText(d.m, d.p, false)", {"m": MAP, "p": 4980.0})
    assert "between 4950 TRAPDOOR (30 below) and 5010 ACCELERANT + PIN (30 above)" in between.replace("PIN + ACCELERANT", "ACCELERANT + PIN") or "4950 TRAPDOOR (30 below)" in between
    assert js("T.nowText(d.m, d.p, false)", {"m": MAP, "p": None}) == ""
    assert js("T.nowText(d.m, d.p, false)", {"m": {"typical_move": 5, "roads": []}, "p": 100}) == "No levels to watch on this map."


def test_breakdown_points_never_take_the_go_green_and_other_roles_keep_the_road_colour():
    assert js("T.roleColor('TRAPDOOR', '#00e070')") == js("T.WARN") != "#00e070"
    assert js("T.roleColor('LAST SHELF', '#00e070')") == js("T.WARN")
    assert js("T.roleColor('BRAKE', '#ff8000')") == "#ff8000"


def test_every_role_has_a_gloss_a_watch_line_and_an_emoji_and_none_tells_you_to_trade():
    roles = js("Object.keys(T.TM_ROLES)")
    assert set(roles) == set(js("Object.keys(T.TM_WATCH)")) == set(js("Object.keys(T.TM_EMO)"))
    text = " ".join(js("Object.values(T.TM_WATCH)") + [v[1] for v in js("Object.values(T.TM_ROLES)")]).lower()
    assert "you should buy" not in text and "sell now" not in text
