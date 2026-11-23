"""Round 38: display preferences the desk agent may set. Allowlist, fixed refusal codes, no path to the guard, absent from a recorder."""
import ast
import asyncio
import json
import pathlib
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from projectx_desk import viewprefs as vp
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub
from test_px_mcp import make
from test_px_recorder_mode import FakeClient as RecFake

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "projectx_desk" / "static"
EXPECTED_KEYS = {"gex", "chartMore", "tmap", "trend", "trend_1m", "trend_5m", "trend_15m"}


@pytest.fixture(autouse=True)
def _file(monkeypatch, tmp_path):
    monkeypatch.setenv("PX_VIEW_FILE", str(tmp_path / "view.json"))


def test_the_key_allowlist_is_exactly_chart_display_flags():
    assert set(vp.KEYS) == EXPECTED_KEYS
    assert all(v.startswith("px.") for v in vp.KEYS.values())
    for k in vp.KEYS:   # nothing that names an order, a ticket, the halt, sizing or a limit
        assert not any(w in k.lower() for w in ("order", "ticket", "halt", "size", "limit", "stop", "risk", "place", "flat"))


def test_set_accepts_booleans_and_words_and_nothing_else(tmp_path):
    v = vp.ViewPrefs(tmp_path / "p.json")
    assert v.set("gex", False) == (True, "ok") and v.prefs == {"gex": False}
    assert v.set("tmap", "on") == (True, "ok") and v.prefs["tmap"] is True
    for bad in (None, 1.5, "maybe", [], {}, 2, ""):
        assert v.set("gex", bad) == (False, "bad_value")
    assert v.prefs == {"gex": False, "tmap": True}


@pytest.mark.parametrize("key", ["halt", "qty", "PX_TRADING", "", None, "gex ", "GEX", "px.gex", 7, "../x"])
def test_unknown_key_is_refused_with_a_fixed_code_and_changes_nothing(tmp_path, key):
    v = vp.ViewPrefs(tmp_path / "p.json")
    assert v.set(key, True) == (False, "unknown_key")
    assert v.prefs == {} and not (tmp_path / "p.json").exists()


def test_persists_atomically_and_a_garbled_file_is_ignored(tmp_path):
    p = tmp_path / "p.json"
    v = vp.ViewPrefs(p)
    v.set("trend_5m", True)
    assert json.loads(p.read_text()) == {"trend_5m": True} and not (tmp_path / "p.json.tmp").exists()
    assert vp.ViewPrefs(p).prefs == {"trend_5m": True}
    p.write_text('{"gex": "x", "halt": true, "tmap": false}')
    assert vp.ViewPrefs(p).prefs == {"tmap": False}          # unknown key and non-boolean dropped on load
    p.write_text("not json")
    assert vp.ViewPrefs(p).prefs == {}


def test_an_unwritable_file_keeps_the_change_in_memory(tmp_path):
    (tmp_path / "blocker").write_text("x")
    v = vp.ViewPrefs(tmp_path / "blocker" / "p.json")
    assert v.set("gex", True) == (True, "ok") and v.prefs == {"gex": True}


def test_view_block_lists_only_set_keys():
    v = vp.ViewPrefs(None)
    assert v.view()["prefs"] == {}
    v.set("trend", False)
    assert v.view() == {"prefs": {"trend": False}, "keys": list(vp.KEYS)}


def _imports(path):
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add(n.module or "")
            out |= {(n.module or "") + "." + a.name for a in n.names}
    return out


def test_viewprefs_imports_no_guard_client_hub_mcp_or_app():
    names = _imports(ROOT / "projectx_desk" / "viewprefs.py")
    assert names <= {"json", "os", "pathlib", "pathlib.Path"}, names
    src = (ROOT / "projectx_desk" / "viewprefs.py").read_text()
    assert ".write(" not in src


def test_the_guard_never_reads_view_prefs():
    for f in ("guard.py", "limits.py", "sizing.py", "anchor.py"):
        s = (ROOT / "projectx_desk" / f).read_text()
        assert "viewprefs" not in s and "set_view" not in s, f


def test_set_view_tool_over_mcp(tmp_path):
    app, *_ = make(tmp_path)
    with TestClient(app) as c:
        async def go():
            async with Client(app.state.mcp) as m:
                names = {t.name for t in await m.list_tools()}
                ok = (await m.call_tool("set_view", {"key": "gex", "value": False})).data
                bad_key = (await m.call_tool("set_view", {"key": "halt", "value": True})).data
                bad_val = (await m.call_tool("set_view", {"key": "gex", "value": "perhaps"})).data
                return names, ok, bad_key, bad_val
        names, ok, bad_key, bad_val = asyncio.run(go())
        assert "set_view" in names
        assert ok["ok"] is True and ok["code"] == "ok" and ok["value"] is False
        assert bad_key["ok"] is False and bad_key["code"] == "unknown_key"
        assert bad_val["ok"] is False and bad_val["code"] == "bad_value"
        assert set(bad_key["keys"]) == EXPECTED_KEYS
        assert not app.state.guard.halted()                    # a key named "halt" did nothing
        assert c.get("/state.json").json()["view"]["prefs"] == {"gex": False}


def test_state_json_is_read_only_and_there_is_no_view_route(tmp_path):
    app, *_ = make(tmp_path)
    paths = {r.path for r in app.routes}
    assert not [p for p in paths if "view" in p and "preview" not in p]
    with TestClient(app) as c:
        assert c.get("/state.json").json()["view"]["prefs"] == {}
        assert c.get("/state.json?stream=1").json()["view"]["prefs"] == {}


def test_a_recorder_has_no_view_prefs_no_mcp_and_no_state_json():
    fake = RecFake()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"), allowed_hosts={"testserver"},
                     connect=False, record_only=True, mcp_token="9f2c61b7a04e83d5c7b1e690a3f48d2e5b7c9a10d6e4f3b2a1c8d7e6f5a49b30")
    assert app.state.view is None and not hasattr(app.state, "mcp")
    assert "/state.json" not in {r.path for r in app.routes}


def test_deskagent_lists_set_view_only_behind_its_own_switch(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import deskagent as d
    monkeypatch.setattr(d, "mcp_config", lambda: "/x/mcp.json")
    monkeypatch.setattr(d, "ROOT", pathlib.Path("/nonexistent"))
    monkeypatch.delenv("PX_AGENT_TRAIL", raising=False)
    monkeypatch.delenv("PX_AGENT_VIEW", raising=False)
    assert "mcp__deskmcp__set_view" not in d.build_cmd("q")
    monkeypatch.setenv("PX_AGENT_VIEW", "1")
    cmd = d.build_cmd("q")
    assert "mcp__deskmcp__set_view" in cmd and "mcp__deskmcp__arm_trail" not in cmd
    assert d.VIEW_TOOLS == ["mcp__deskmcp__set_view"]
    assert "mcp__deskmcp__set_view" not in d.DENIED


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _node(expr):
    out = subprocess.run(["node", "-e", "const V=require(process.argv[1]);console.log(JSON.stringify(" + expr + "))", str(STATIC / "viewprefs.js")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_js_keys_match_the_server_allowlist():
    assert set(_node("V.KEYS")) == set(vp.KEYS)


@needs_node
def test_js_diff_and_sig_ignore_unknown_and_non_boolean():
    assert _node("V.diff({gex:false,tmap:true,halt:true,trend:'x'},{gex:true,tmap:true,trend:true,halt:false})") == ["gex"]
    assert _node("V.diff(null,{gex:true})") == [] and _node("V.diff({gex:true},null)") == []
    assert _node("V.sig({trend:true,gex:false,halt:true})") == "gex=0,trend=1"
    assert _node("V.sig(undefined)") == ""


def test_the_static_file_has_no_network_or_storage():
    s = (STATIC / "viewprefs.js").read_text()
    for w in ("fetch", "XMLHttpRequest", "localStorage", "sendBeacon", "WebSocket", "/api/"):
        assert w not in s, w
