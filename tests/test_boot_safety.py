"""Boot safety: refuse a missing or unmounted data dir before the lock, opt-in sentinel, deadline-loop heartbeat in /state.json."""
import ast
import asyncio
import pathlib
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from projectx_desk import app as app_mod
from projectx_desk import datacheck
from projectx_desk.app import create_app, _deadline_loop
from projectx_desk.hub import SignalRHub
from tests.test_px_app import FakeClient

PKG = pathlib.Path(app_mod.__file__).parent


def test_missing_configured_halt_parent_refuses_without_creating_it(tmp_path):
    missing = tmp_path / "pool" / "px"
    msg = datacheck.problem({"PX_HALT_FILE": str(missing / "HALT")}, tmp_path)
    assert msg and "PX_HALT_FILE" in msg and str(missing) in msg and f"mkdir -p {missing}" in msg
    assert not missing.exists() and not (tmp_path / "pool").exists()


def test_dangling_symlink_data_dir_refuses(tmp_path):
    (tmp_path / "data").symlink_to(tmp_path / "gone")
    msg = datacheck.problem({}, tmp_path)
    assert msg and "symlink" in msg and str(tmp_path / "data") in msg


def test_dangling_symlink_override_parent_refuses(tmp_path):
    link = tmp_path / "pool"
    link.symlink_to(tmp_path / "gone")
    assert datacheck.problem({"PX_AUDIT_PATH": str(link / "audit.jsonl")}, tmp_path)


def test_fresh_checkout_without_data_dir_boots(tmp_path):
    assert not (tmp_path / "data").exists()
    assert datacheck.problem({}, tmp_path) is None
    assert not (tmp_path / "data").exists()           # a pure check creates nothing


def test_missing_checkout_root_refuses(tmp_path):
    assert datacheck.problem({}, tmp_path / "nope")


def test_default_and_existing_override_paths_unaffected(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "other").mkdir()
    env = {"PX_HALT_FILE": str(tmp_path / "other" / "HALT"), "PX_LOCK_FILE": str(tmp_path / "other" / "x.lock")}
    assert datacheck.problem({}, tmp_path) is None
    assert datacheck.problem(env, tmp_path) is None


def test_sentinel_required_when_on_and_absent(tmp_path):
    (tmp_path / "data").mkdir()
    msg = datacheck.problem({"PX_DATA_SENTINEL": "1"}, tmp_path)
    assert msg and "python -m projectx_desk --init-data" in msg


def test_sentinel_present_boots(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".desk_data").write_text("x")
    assert datacheck.problem({"PX_DATA_SENTINEL": "1"}, tmp_path) is None


@pytest.mark.parametrize("v", [None, "", "0", "yes", "garbled"])
def test_sentinel_flag_off_or_garbled_boots(tmp_path, v):
    (tmp_path / "data").mkdir()
    env = {} if v is None else {"PX_DATA_SENTINEL": v}
    assert datacheck.problem(env, tmp_path) is None


def test_init_data_writes_sentinel_and_main_exits_zero(tmp_path, monkeypatch):
    s = datacheck.init_data(tmp_path)
    assert s == tmp_path / "data" / ".desk_data" and s.exists()
    assert datacheck.problem({"PX_DATA_SENTINEL": "1"}, tmp_path) is None
    monkeypatch.setattr(app_mod, "ROOT", tmp_path / "r")
    (tmp_path / "r").mkdir()
    monkeypatch.setattr(sys, "argv", ["projectx_desk", "--init-data"])
    monkeypatch.setenv("PX_NO_ENV_FILE", "1")
    monkeypatch.delenv("PX_RECORD_ONLY", raising=False)
    with pytest.raises(SystemExit) as e:
        app_mod.main()
    assert e.value.code == 0 and (tmp_path / "r" / "data" / ".desk_data").exists()


def test_new_code_passes_the_guard_pin():
    from tests import test_px_guard_pin as pin
    for f in (PKG / "datacheck.py", PKG / "app.py"):
        assert not pin.violations(f.read_text(encoding="utf-8"), f"projectx_desk/{f.name}")


def test_refusal_messages_carry_no_env_values_or_tokens(tmp_path):
    secret = "tok_SECRET_value_123"
    env = {"PX_HALT_FILE": str(tmp_path / "gone" / "HALT"), "PX_MCP_TOKEN": secret, "PX_OPERATOR_TOKEN": secret,
           "PROJECTX_API_KEY": secret, "PX_DATA_SENTINEL": "1"}
    assert secret not in datacheck.problem(env, tmp_path)
    (tmp_path / "gone").mkdir()
    assert secret not in datacheck.problem(env, tmp_path)


def test_datacheck_imports_nothing_from_the_package():
    tree = ast.parse((PKG / "datacheck.py").read_text(encoding="utf-8"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in mods if "projectx_desk" in m]


def test_main_is_the_only_caller_of_the_check():
    tree = ast.parse((PKG / "app.py").read_text(encoding="utf-8"))
    callers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "px_datacheck":
                    callers.add(fn.name)
    assert "main" in callers and "create_app" not in callers
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    src = ast.unparse(main)
    assert src.index("px_datacheck.problem") < src.index("px_lock.acquire")


def _app(**kw):
    fake = FakeClient()
    hub = SignalRHub(fake, "market", url="ws://127.0.0.1:1/x")
    uhub = SignalRHub(fake, "user", url="ws://127.0.0.1:1/y")
    return create_app(client=fake, market_hub=hub, user_hub=uhub, allowed_hosts={"testserver"}, connect=False, **kw)


def test_state_json_loops_none_before_first_tick_then_a_growing_number():
    app = _app()
    with TestClient(app) as c:
        assert c.get("/state.json").json()["loops"] == {"deadline_age_s": None}
        app.state.loop_tick = time.monotonic() - 3
        a = c.get("/state.json").json()["loops"]["deadline_age_s"]
        app.state.loop_tick = time.monotonic() - 9
        b = c.get("/state.json").json()["loops"]["deadline_age_s"]
        assert isinstance(a, float) and 3 <= a < 6 and b > a


def test_record_only_state_has_none():
    fake = FakeClient()
    hub = SignalRHub(fake, "market", url="ws://127.0.0.1:1/x")
    app = create_app(client=fake, market_hub=hub, allowed_hosts={"testserver"}, connect=False, record_only=True)
    with TestClient(app):
        assert "/state.json" not in {getattr(r, "path", "") for r in app.routes}      # record-only has no state route at all
        assert app_mod._deadline_age(app) is None                                      # and no deadline task: never an error


def test_deadline_loop_sets_the_tick_at_the_top_of_each_iteration():
    app = types.SimpleNamespace(state=types.SimpleNamespace(guard=None))

    async def run():
        t = asyncio.create_task(_deadline_loop(app))
        await asyncio.sleep(0.05)
        first = app.state.loop_tick
        await asyncio.sleep(1.2)
        second = app.state.loop_tick
        t.cancel()
        return first, second
    first, second = asyncio.run(run())
    assert second > first
