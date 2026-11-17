"""Phase 0, task 4: capture and drift scripts, run_dev wiring, docs hygiene."""
import importlib.util
import json
import pathlib
import re

import httpx
import pytest

from projectx_desk import dev_robinhood
from projectx_desk.rh import tokens
from tests.test_px_guard_pin import PKG, ROOT, violations
from tests.test_rh_reads import MCP_ACCT, OTHER_ACCT


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cap, drift = load("rh_capture"), load("rh_drift")


def _write(tmp_path, name, tools):
    p = tmp_path / name
    p.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    return str(p)


def tool(name, required=()):
    return {"name": name, "description": "d", "input_schema": {"type": "object", "required": list(required)}}


BASE = [tool("get_accounts"), tool("get_equity_positions", ["account_number"]), tool("get_portfolio", ["account_number"])]


def test_drift_no_change(tmp_path, capsys):
    a = _write(tmp_path, "a.json", BASE)
    assert drift.main([a, a]) == 0


def test_drift_removed_called_tool(tmp_path):
    a, b = _write(tmp_path, "a.json", BASE), _write(tmp_path, "b.json", BASE[:1] + BASE[2:])
    assert drift.main([a, b]) == 1


def test_drift_new_required_input(tmp_path, capsys):
    new = [BASE[0], tool("get_equity_positions", ["account_number", "market"]), BASE[2]]
    assert drift.main([_write(tmp_path, "a.json", BASE), _write(tmp_path, "b.json", new)]) == 1
    assert "market" in capsys.readouterr().out


def test_drift_added_tool_and_uncalled_removal_do_not_fail(tmp_path):
    old = BASE + [tool("legacy_thing")]
    new = BASE + [tool("brand_new")]
    assert drift.main([_write(tmp_path, "a.json", old), _write(tmp_path, "b.json", new)]) == 0


def test_drift_bad_args_and_files(tmp_path):
    assert drift.main([]) == 2
    assert drift.main([str(tmp_path / "nope.json"), str(tmp_path / "nope2.json")]) == 2


async def test_capture_against_mock_has_no_account_data(tmp_path):
    app = dev_robinhood.make_app()
    st = app.state.mock
    pair = st.issue("c")
    import time
    store = tokens.TokenStore(tmp_path / "t.json", base_url="http://127.0.0.1/mcp", transport=httpx.ASGITransport(app=app))
    store.save(tokens.Tokens(pair["access_token"], pair["refresh_token"], time.time() + 3600, "c"))
    out = await cap.capture("http://127.0.0.1/mcp", store, transport=httpx.ASGITransport(app=app))
    names = {t["name"] for t in out}
    assert {"get_accounts", "place_equity_order"} <= names
    p = tmp_path / "tools.json"
    cap.write_atomic(p, {"tools": out})
    blob = p.read_text()
    for banned in (MCP_ACCT, OTHER_ACCT, "account_number\": \"", pair["access_token"], pair["refresh_token"]):
        assert banned not in blob
    assert oct(p.stat().st_mode & 0o777)  # file exists
    assert drift.main([str(p), str(p)]) == 0


def test_sanitize_drops_values():
    t = cap.sanitize([{"name": "x", "description": "d", "inputSchema": {
        "type": "object", "required": ["a"],
        "properties": {"a": {"type": "string", "default": "ACCT-1", "examples": ["ACCT-2"], "enum": ["u", "v"]}}}}])
    s = json.dumps(t)
    assert "ACCT" not in s and '"enum": ["u", "v"]' in s and t[0]["input_schema"]["required"] == ["a"]


# ---- the pin ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["place_equity_order", "cancel_equity_order", "place_option_order", "cancel_option_order",
                                  "exercise_option", "decline_trade_approval"])
def test_pin_fails_on_a_robinhood_write_tool_name_in_a_non_guard_module(name):
    assert violations(f'TOOL = "{name}"', f"{PKG}/rh/reads.py")
    assert violations(f'await c.call("{name}", {{}})', f"{PKG}/app.py")
    assert violations(f'TOOL = "{name}"', f"{PKG}/rh/guard.py") == []      # the second allowed writer
    assert violations(f'TOOL = "{name}"', f"{PKG}/dev_robinhood.py") == []  # server-side mock, exempt like dev_gateway


def test_pin_allows_read_tool_names():
    assert violations('c.call("get_equity_positions", {})', f"{PKG}/rh/reads.py") == []


def test_no_dashes_in_the_new_files():
    files = [ROOT / "scripts" / "rh_capture.py", ROOT / "scripts" / "rh_drift.py", ROOT / "docs" / "ROBINHOOD.md",
             ROOT / "run_dev.sh", ROOT / "run_dev.ps1"]
    for f in files:
        assert not re.search("[–—]", f.read_text(encoding="utf-8")), f


def test_run_dev_scripts_start_the_mock():
    for n in ("run_dev.sh", "run_dev.ps1"):
        t = (ROOT / n).read_text(encoding="utf-8")
        assert "PX_RH" in t and "PX_RH_MOCK" in t, n


def test_robinhood_doc_has_the_required_sections():
    t = (ROOT / "docs" / "ROBINHOOD.md").read_text(encoding="utf-8")
    low = t.lower()
    assert "no paper trading" in low and "trade approvals" in low and "dedicated" in low
    assert "ssh -L" in t and "localhost" in low
    assert t.count("tradermatrix.pro") == 1 and "https://tradermatrix.pro?ref=MPHINANCE" in t
    assert t.rstrip().splitlines()[-1].find("tradermatrix.pro") >= 0 or "tradermatrix.pro" in t.split("---")[-1]
