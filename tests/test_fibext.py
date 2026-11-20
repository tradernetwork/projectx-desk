"""The trader's own Fibonacci extension drawing (static/fibext.js, pure, node) plus the wiring pins that keep it display only."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
JS = (STATIC / "desk.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
FX = (STATIC / "fibext.js").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def js(expr, payload=None):
    code = f"const T=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code, str(STATIC / "fibext.js")], input=json.dumps(payload or {}), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_upward_leg_projects_up_from_the_third_point():
    got = js("T.fibExtLevels(d.a, d.b, d.c)", {"a": 100, "b": 110, "c": 105})
    assert [g["ratio"] for g in got] == [0, 0.618, 1, 1.272, 1.618, 2, 2.618]
    assert [round(g["price"], 4) for g in got] == [105, 111.18, 115, 117.72, 121.18, 125, 131.18]


@needs_node
def test_downward_leg_projects_down_from_the_third_point():
    got = js("T.fibExtLevels(d.a, d.b, d.c)", {"a": 7700, "b": 7650, "c": 7680})
    assert [round(g["price"], 4) for g in got] == [7680, 7649.1, 7630, 7616.4, 7599.1, 7580, 7549.1]


@needs_node
def test_missing_or_non_finite_points_give_no_levels_never_a_guess():
    assert js("T.fibExtLevels(d.a, d.b, d.c)", {"a": 100, "b": None, "c": 105}) == []
    assert js("T.fibExtLevels(100, NaN, 105)") == []
    assert js("T.fibExtLevels(100, Infinity, 105)") == []
    assert js("T.fibExtLevels('100', 110, 105)") == []


@needs_node
def test_label_uses_chart_precision_and_trims_the_ratio():
    assert js("T.fibExtLabel(7712.5, 1.618, 2)") == "7712.50 (1.618)"
    assert js("T.fibExtLabel(7712.5, 2, 2)") == "7712.50 (2)"
    assert js("T.fibExtLabel(7712.5, 0.618, 0)") == "7713 (0.618)"
    assert js("T.fibExtLabel(NaN, 1, 2)") == ""


def test_registered_under_a_unique_name_and_is_a_trader_drawing():
    assert len(re.findall(r"name: 'userFibExt'", JS)) == 1
    assert not re.search(r"registerOverlay\(\{\s*name: 'fibonacciExtension'", JS)       # the bare name is never taken from the registry
    assert "['FibX', 'userFibExt']" in JS and "x: 'userFibExt'" in JS
    nonuser = re.search(r"const NONUSER = \[([^\]]*)\]", JS).group(1)
    assert "userFibExt" not in nonuser                                                  # saved with the trader's other drawings
    assert '<script src="/static/fibext.js"></script>' in HTML
    assert HTML.index("/static/fibext.js") < HTML.index("/static/klinecharts.min.js")


def test_saved_and_restored_like_every_other_drawing_by_name():
    save = JS[JS.index("function saveOverlays()"):JS.index("function toolbar()")]
    assert "o.name" in save and "o.name ===" not in save.replace("o.name !== 'measure'", "")      # no per-name whitelist: any registered name round-trips
    assert "mkOv({name: o.name, points: o.points" in save                                      # restore recreates by the saved name
    assert "o.points?.length >= (o.totalStep - 1 || 1)" in save                                  # 3 placed points of totalStep 4 qualify


def test_drawing_path_has_no_request_and_the_pure_file_has_no_io():
    start = JS.index("name: 'userFibExt'")
    block = JS[start:JS.index("/* horizontal level line", start)]
    tool = JS[JS.index("/* ---- drawing toolbar ---- */"):JS.index("/* ---- indicators:")]
    for src in (block, tool, FX):
        assert not re.search(r"fetch\(|XMLHttpRequest|sendBeacon|method:\s*'POST'|\bpost\(|WebSocket|EventSource", src), src[:60]
    assert "document" not in FX and "window." not in FX.replace("typeof window", "")
