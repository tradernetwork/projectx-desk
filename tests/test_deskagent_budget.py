"""scripts/deskagent.py: per-question cost cap, fixed messages instead of raw output, capped result, private atomic sessions file."""
import json
import os
import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import deskagent as d  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "mcp_config", lambda: "/x/mcp.json")
    monkeypatch.setattr(d, "ROOT", tmp_path)            # no .env.projectx here
    monkeypatch.delenv("PX_AGENT_BUDGET_USD", raising=False)


def test_budget_flag_default_and_override(monkeypatch):
    cmd = d.build_cmd("q")
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1"
    monkeypatch.setenv("PX_AGENT_BUDGET_USD", "0.25")
    cmd = d.build_cmd("q")
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.25"


def test_budget_zero_is_off_and_garbled_falls_back(monkeypatch):
    monkeypatch.setenv("PX_AGENT_BUDGET_USD", "0")
    assert "--max-budget-usd" not in d.build_cmd("q")
    for bad in ("abc", "-3", "nan", "inf", " "):
        monkeypatch.setenv("PX_AGENT_BUDGET_USD", bad)
        assert d.budget_usd() == 1.0, bad


def test_strict_mcp_config_denied_list_and_no_plan_mode():
    cmd = d.build_cmd("q", "sess-1")
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--mcp-config") + 1] == "/x/mcp.json"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "plan" not in cmd and "--no-session-persistence" not in cmd and cmd[-2:] == ["--resume", "sess-1"]
    for t in ("Bash", "Edit", "Write", "Agent", "Read(./data/agent_token)", "Read(./data/rh_*)"):
        assert t in cmd[cmd.index("--disallowedTools"):]
    assert len(d.DENIED) == 23


def test_result_capped_and_fixed_messages():
    assert len(d.reply_text({"result": "x" * 50_000})) == d.MAX_RESULT
    assert d.reply_text({"subtype": "error_max_budget_usd"}) == d.MSG_BUDGET
    assert d.reply_text({"result": "  "}) == d.MSG_EMPTY and d.reply_text({}) == d.MSG_EMPTY
    assert d.reply_text(["x"]) == d.MSG_FAILED
    assert all(m.strip() for m in (d.MSG_BUDGET, d.MSG_EMPTY, d.MSG_FAILED))


def test_unparseable_output_never_echoed(monkeypatch):
    class R:
        stdout, stderr = "Authorization: Bearer SECRETVALUE", "token=SECRETVALUE"
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(d, "load_sessions", lambda: {})
    out = d.ask("q")
    assert out == d.MSG_FAILED and "SECRET" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_sessions_file_private_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "DATA", tmp_path / "data")
    monkeypatch.setattr(d, "SESSIONS", tmp_path / "data" / "agent_sessions.json")
    (tmp_path / "data").mkdir()
    stale = tmp_path / "data" / "agent_sessions.json.tmp"
    stale.write_text("old")
    os.chmod(stale, 0o644)
    d.save_sessions({"desk": "abc"})
    assert stat.S_IMODE(d.SESSIONS.stat().st_mode) == 0o600
    assert json.loads(d.SESSIONS.read_text()) == {"desk": "abc"} and not stale.exists()
    assert not any(isinstance(n, __import__("ast").Attribute) and n.attr == "write"
                   for n in __import__("ast").walk(__import__("ast").parse((pathlib.Path(d.__file__)).read_text())))
