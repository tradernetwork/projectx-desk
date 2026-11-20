"""Robinhood panel (phase 1, task 4): the demo flag, the pure formatting in static/rh.js (run under node), and static pins."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from projectx_desk.rh import state as rh_state
from tests.test_rh_app import mock_runtime

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "projectx_desk" / "static"
JS = STATIC / "rh.js"
CSS = STATIC / "rh.css"
NODE = shutil.which("node")


def read(p):
    return pathlib.Path(p).read_text(encoding="utf-8")


# ---- the DEMO flag -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("val,want", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("garbled", False), (None, False)])
def test_demo_flag_follows_px_rh_demo(tmp_path, val, want):
    env = {"PX_RH": "1", "PX_RH_TOKEN_FILE": str(tmp_path / "t.json")}
    if val is not None:
        env["PX_RH_DEMO"] = val
    rt = rh_state.from_env(env)
    assert rt.state.snapshot()["demo"] is want                     # before the first poll
    assert rt.state.snapshot(stream=True)["demo"] is want


@pytest.mark.asyncio
async def test_demo_is_in_a_polled_snapshot_and_defaults_false():
    rt = mock_runtime()
    await rt.poll_once()
    assert rt.state.snapshot()["demo"] is False
    rt.state.demo = True
    assert rt.state.snapshot()["demo"] is True and rt.state.snapshot(stream=True)["demo"] is True


# ---- static pins ------------------------------------------------------------------------------------------------
def test_files_exist_and_index_has_exactly_one_tag_each():
    html = read(STATIC / "index.html")
    assert html.count('<script src="/static/rh.js"></script>') == 1
    assert html.count('<link rel="stylesheet" href="/static/rh.css">') == 1


def test_no_em_or_en_dashes_anywhere_in_the_new_files():
    for p in (JS, CSS, pathlib.Path(__file__)):
        assert not re.search("[" + chr(0x2013) + chr(0x2014) + "]", read(p)), p.name


def test_no_dynamic_html_eval_storage_or_console():
    src = read(JS)
    code = "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())      # comments may name what is banned
    for bad in (r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML", r"\beval\s*\(", r"new\s+Function", r"document\.write",
                r"localStorage", r"sessionStorage", r"indexedDB", r"\bconsole\b", r"document\.cookie", r"setTimeout\s*\(\s*['\"]", r"XMLHttpRequest", r"sendBeacon"):
        assert not re.search(bad, code), bad


def test_only_rh_routes_and_csrf_are_fetched():
    src = read(JS)
    urls = set(re.findall(r"""['"](/api/[^'"?]*)['"]""", src))
    assert urls and all(u == "/api/csrf" or u.startswith("/api/rh/") for u in urls), urls
    assert {"/api/csrf", "/api/rh/state", "/api/rh/tickets", "/api/rh/preview", "/api/rh/place", "/api/rh/cancel", "/api/rh/reconcile",
            "/api/rh/release", "/api/rh/discard", "/api/rh/halt/resolve"} <= urls
    assert not re.search(r"https?://", src)                                           # no external host


def test_no_other_script_or_the_guard_files_are_touched_by_it():
    assert "desk.js" not in read(JS).replace("never touches desk.js", "")
    html = read(STATIC / "index.html")
    assert html.count("rh.js") == 1


def test_every_refused_code_has_a_plain_words_mapping():
    guard = read(ROOT / "projectx_desk" / "rh" / "guard.py")
    raised = set(re.findall(r'Refused\(\s*"([a-z_]+)"', guard))
    block = re.search(r"CODES = \((.*?)\)\n", guard, re.S).group(1)
    declared = set(re.findall(r'"([a-z_]+)"', block))
    assert raised and raised <= declared
    js = read(JS)
    mapped = set(re.findall(r"^\s{4}([a-z_]+): '", js[js.index("const REFUSALS"):js.index("const REFUSAL_FALLBACK")], re.M))
    assert declared <= mapped, declared - mapped


def test_every_ticket_state_has_a_plain_words_line():
    guard = read(ROOT / "projectx_desk" / "rh" / "guard.py")
    states = set()
    for m in re.finditer(r"^([A-Z_, ]+) = ((?:\"[a-z_]+\",? ?)+)$", guard, re.M):
        states |= set(re.findall(r'"([a-z_]+)"', m.group(2)))
    states &= {"previewed", "sending", "awaiting_approval", "approved", "placed", "unknown", "rejected", "refused", "discarded",
               "declined", "expired", "filled", "cancelled", "released"}
    js = read(JS)
    mapped = set(re.findall(r"^\s{4}([a-z_]+): '", js[js.index("const STATES"):js.index("function stateText")], re.M))
    assert states and states <= mapped, states - mapped


# ---- the pure functions, under node ------------------------------------------------------------------------------
def run_node(expr):
    code = f"const r=require({json.dumps(str(JS))});process.stdout.write(JSON.stringify({expr}))"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@needs_node
def test_refusal_and_state_words():
    assert "Robinhood app" in run_node("r.refusalText('approvals_off')")
    assert run_node("r.refusalText('made_up')") == "The desk refused it."
    assert run_node("r.refusalText('__proto__')") == "The desk refused it."
    assert "approve it in the Robinhood app" in run_node("r.stateText('awaiting_approval')")
    assert "reconcile" in run_node("r.stateText('unknown')").lower() and "halted" in run_node("r.stateText('unknown')")
    assert run_node("r.WARNING_LINE") == ("No paper trading. This is real money. A fill is unprotected: the desk places no stop. "
                                          "Approve in the Robinhood app.")
    assert run_node("r.DEMO_LINE") == "DEMO: mock Robinhood, no real account"


@needs_node
def test_actions_allowed_per_state():
    assert run_node("r.actionsFor('unknown')") == {"cancel": False, "reconcile": True, "release": True}
    assert run_node("r.actionsFor('awaiting_approval')") == {"cancel": True, "reconcile": False, "release": True}
    assert run_node("r.actionsFor('filled')") == {"cancel": False, "reconcile": False, "release": False}
    assert run_node("r.actionsFor('previewed')") == {"cancel": False, "reconcile": False, "release": False}
    assert run_node("r.isLive('placed')") is True and run_node("r.isLive('declined')") is False


@needs_node
def test_formatting():
    assert run_node("[r.fmtMoney(1234.5), r.fmtMoney(-7), r.fmtMoney(null), r.fmtMoney(NaN)]") == ["$1,234.50", "-$7.00", "?", "?"]
    assert run_node("[r.fmtQty(3), r.fmtQty(2.5), r.fmtQty('x')]") == ["3", "2.5", "?"]
    assert run_node("r.optionLabel({symbol:'AAA',expiry:'2026-12-18',strike:150,right:'call'})") == "AAA 2026-12-18 150 C"
    assert run_node("r.optionLabel({symbol:'AAA',right:'put'})") == "AAA ? ? P"
    assert run_node("[r.countdown(100,40), r.countdown(100,100), r.countdown(100,200), r.countdown(null,1)]") == ["1:00", "expired", "expired", ""]
    assert run_node("r.countdown(100.5,40)") == "1:01"
    line = run_node("r.accountLine({type:'margin',state:'active',option_level:'option_level_2'},{human_must_approve:false})")
    assert "OFF" in line and "margin" in line and not re.search(r"\d{5,}", line)
    assert "unknown" in run_node("r.accountLine({type:'cash',state:'active'},{human_must_approve:null})")


@needs_node
def test_form_reading():
    ok = run_node("r.readForm({symbol:' ddd ',side:'buy',qty:'2',limit:'101.25'})")
    assert ok == {"ok": True, "intent": {"symbol": "DDD", "side": "buy", "qty": 2, "limit_price": 101.25}}
    for f in ("{symbol:'',side:'buy',qty:'1',limit:'1'}", "{symbol:'A',side:'short',qty:'1',limit:'1'}", "{symbol:'A',side:'buy',qty:'1.5',limit:'1'}",
              "{symbol:'A',side:'buy',qty:'0',limit:'1'}", "{symbol:'A',side:'buy',qty:'1',limit:'0'}", "{symbol:'A',side:'buy',qty:'1',limit:'-3'}",
              "{symbol:'A B',side:'buy',qty:'1',limit:'1'}", "{symbol:'A',side:'buy',qty:'1e3',limit:'1'}", "{symbol:'A',side:'buy',qty:'1',limit:'Infinity'}", "null"):
        assert run_node(f"r.readForm({f})") == {"ok": False, "code": "bad_intent"}, f


@needs_node
def test_hold_progress_is_600ms():
    assert run_node("[r.HOLD_MS, r.holdProgress(0), r.holdProgress(300), r.holdProgress(600), r.holdProgress(9999), r.holdProgress(-5)]") == [600, 0, 0.5, 1, 1, 0]


# ---- the LETF desk wiring (static/letf.js is covered by tests/test_letf_ui.py) ---------------------------------------------
def test_index_has_one_letf_tag_before_rh_js():
    html = read(STATIC / "index.html")
    assert html.count('<script src="/static/letf.js"></script>') == 1
    assert html.index("/static/letf.js") < html.index("/static/rh.js")             # rh.js mounts it at boot


@needs_node
def test_with_stream_handles_a_path_that_already_has_a_query():
    assert run_node("[r.withStream('/api/rh/state', true), r.withStream('/api/rh/bars?symbols=QQQ', true), r.withStream('/api/rh/state', false)]") \
        == ["/api/rh/state?stream=1", "/api/rh/bars?symbols=QQQ&stream=1", "/api/rh/state"]


def test_prefill_only_fills_boxes_and_never_posts():
    src = read(JS)
    m = re.search(r"function prefill\(sym\)\{.*?\n  \}\n  function useAsk\(price\)\{.*?\n  \}\n", src, re.S)
    assert m, "prefill and useAsk not found"
    body = m.group(0)
    for bad in (r"\bpost\(", r"\bfetch\(", r"\bact\(", r"/api/", r"\bpreview\b", r"\bplace\(", r"placeNow", r"readForm", r"\.onsubmit", r"\.submit\("):
        assert not re.search(bad, body), bad                                         # a fund click only sets input values
    assert "f.sym.value" in body and "S.fields.lim.value" in body


def test_the_ticket_form_is_one_node_moved_between_views_not_rebuilt():
    src = read(JS)
    assert "ctl.ticketSlot.append(formHost)" in src and "body.insertBefore(formHost, listHost)" in src
    m = re.search(r"function applyView\(\)\{.*?\n  \}\n", src, re.S)
    assert m and "renderForm" not in m.group(0)                                      # a view switch must not wipe a previewed ticket
