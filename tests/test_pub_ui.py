"""Round 36: the Public tab UI (static/pub.js, pub.css). Pure helpers run under node on the reduced fixture; static pins."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from projectx_desk.pub import view as pub_view
from tests.test_pub_link import FIX

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "projectx_desk" / "static"
JS = STATIC / "pub.js"
CSS = STATIC / "pub.css"
NODE = shutil.which("node")
NOTE = "Display only. Orders happen in the Public desk, not here."


def read(p):
    return pathlib.Path(p).read_text(encoding="utf-8")


def code_only(src):
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def node(expr, view):
    if not NODE:
        pytest.skip("node not installed")
    script = (f"const p = require({json.dumps(str(JS))}); const v = {json.dumps(view)};"
              f"process.stdout.write(JSON.stringify({expr}));")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def full():
    return {**pub_view.reduce(json.loads(read(FIX))), "age_s": 4}


def stream():
    return {**pub_view.reduce(json.loads(read(FIX)), stream=True), "age_s": 4}


# ---- F30: files, tags, the switch ----------------------------------------------------------------------------------
def test_files_exist_and_index_has_exactly_one_tag_each_after_rh():
    html = read(STATIC / "index.html")
    assert html.count('<script src="/static/pub.js"></script>') == 1
    assert html.count('<link rel="stylesheet" href="/static/pub.css">') == 1
    assert html.index('src="/static/rh.js"') < html.index('src="/static/pub.js"')


def test_rh_js_is_untouched_by_the_public_tab():
    src = read(STATIC / "rh.js")
    assert "pxPub" not in src and "Public" not in src and "/api/pub" not in src


def test_switch_joins_the_robinhood_switch_or_draws_its_own():
    src = code_only(read(JS))
    assert "querySelectorAll('.rh-switch')" in src and "'rh-switch pub-own'" in src
    assert "'ProjectX'" in src and "'Public'" in src


# ---- F31: the read-only panel ----------------------------------------------------------------------------------------
def test_the_permanent_note_is_there():
    assert f"const NOTE = '{NOTE}';" in read(JS)
    assert node("p.NOTE", {}) == NOTE


def test_rows_lines_and_unknown_markers_on_the_fixture():
    v = full()
    got = node("{s: p.statusLine(v), a: p.accountLine(v), pos: p.positionRows(v), ord: p.orderRows(v), tk: p.ticketRows(v),"
               " cyc: p.cycleRows(v), wash: p.washRows(v), w: p.wheelLine(v), h: p.haltLine(v)}", v)
    assert got["s"].startswith("data 4 s old. Trading off, options off.")
    assert got["a"] == "Account: BROKERAGE, BUY_AND_SELL, options LEVEL_2. Equity 12345.67, cash 4321.00, buying power 4321.00"
    assert got["pos"]["head"] == ["Symbol", "Asset", "Qty", "Cost", "Value", "Unrealized"]
    assert got["pos"]["rows"][1] == ["ACME261218P00040000", "OPTION", "-1", "UNKNOWN", "-35.00", "UNKNOWN"]
    assert got["ord"]["rows"][0] == ["ZETA", "BUY", "LIMIT", "NEW", "5", "0", "18.25", "UNKNOWN"]
    assert got["tk"]["rows"][0] == ["placed", "ZETA", "BUY", "EQUITY", "5", "-", "18.25"]
    assert got["cyc"]["rows"] == [["ACME", "yes", "100", "41.35", "3"], ["BOLT", "yes", "0", "UNKNOWN", "1"]]
    assert got["wash"]["rows"] == [["BOLT", "POSSIBLE", "2026-10-20 to 2026-12-19"]]
    assert got["w"].startswith("Wheel: 2 cycles open, 1 legs open, 1 with unknown basis")
    assert got["h"] is None


def test_halt_banner_and_unreachable_read_as_unknown():
    v = full()
    v["halt"] = {"halted": True, "cause": "place_unknown"}
    assert node("p.haltLine(v)", v) == "The Public desk is HALTED (cause: place_unknown). Only a human clears it, in the Public desk."
    down = pub_view.failed("timeout")
    got = node("{s: p.statusLine(v), a: p.accountLine(v), pos: p.positionRows(v), cyc: p.cycleRows(v), w: p.wheelLine(v), h: p.haltLine(v)}", down)
    assert got["s"] == "Public desk not readable: the Public desk did not answer within 3 seconds. Everything below is UNKNOWN."
    assert got["a"] == "Account: UNKNOWN" and got["pos"] is None and got["cyc"] is None and got["w"] == "Wheel: UNKNOWN"
    assert got["h"] is None
    v = full()
    v["halt"] = {"halted": None, "cause": None}
    assert node("p.haltLine(v)", v) == "Public desk halt state: UNKNOWN."


def test_stream_view_draws_no_dollar_column_or_value():
    v = stream()
    got = node("[p.accountLine(v), p.positionRows(v), p.orderRows(v), p.ticketRows(v), p.cycleRows(v)]", v)
    text = json.dumps(got)
    for bad in ("Equity", "Cost", "Value", "Unrealized", "Price", "Avg", "Limit", "12345.67", "18.25", "41.35", "4400"):
        assert bad not in text, bad
    assert got[4]["rows"][0][3] == "KNOWN (hidden in stream mode)"


def test_age_and_stream_path():
    assert node("[p.ageText(0), p.ageText(12), p.ageText(null), p.ageText(-1)]", {}) == \
        ["data 0 s old", "data 12 s old", "data age UNKNOWN", "data age UNKNOWN"]
    assert node("[p.withStream(p.ROUTE, true), p.withStream(p.ROUTE, false)]", {}) == ["/api/pub/state?stream=1", "/api/pub/state"]


# ---- F32: what the page may do -----------------------------------------------------------------------------------------
def test_fetches_only_the_one_route_and_never_posts():
    src = code_only(read(JS))
    assert re.findall(r"fetch\(([^\n]*)", src) == ["withStream(ROUTE, streaming()), {credentials: 'same-origin'});"]
    assert set(re.findall(r"'(/[a-z/_.]+)'", src)) == {"/api/pub/state"}
    for bad in (r"\bPOST\b", r"\bPUT\b", r"\bDELETE\b", r"method\s*:", r"/api/csrf", r"x-px-csrf", r"/api/rh/", r"/api/desk", r"/api/wheel"):
        assert not re.search(bad, src), bad


def test_no_dynamic_html_eval_storage_or_console():
    src = code_only(read(JS))
    for bad in (r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML", r"\beval\s*\(", r"new\s+Function", r"document\.write",
                r"localStorage", r"sessionStorage", r"indexedDB", r"\bconsole\b", r"document\.cookie", r"setTimeout\s*\(\s*['\"]",
                r"XMLHttpRequest", r"sendBeacon", r"WebSocket", r"EventSource", r"\.src\s*=", r"\.href\s*="):
        assert not re.search(bad, src), bad


def test_no_em_or_en_dashes_in_the_new_files():
    for p in (JS, CSS, pathlib.Path(__file__)):
        assert not re.search("[" + chr(0x2013) + chr(0x2014) + "]", read(p)), p.name
