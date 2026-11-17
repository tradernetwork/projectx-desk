"""Robinhood phase 1 wiring: Guard.from_env, the app lifespan builds and attaches it, the track loop runs it."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from projectx_desk import app as app_mod
from projectx_desk import dev_robinhood
from projectx_desk.rh import reads, state as rh_state
from projectx_desk.rh.guard import Guard
from projectx_desk.rh.mcp import Client
from tests.test_rh_app import build
from tests.test_rh_reads import Provider


def env_for(tmp_path, **extra):
    return {"PX_RH": "1", "PX_RH_TRADING": "1", "PX_RH_MAX_NOTIONAL": "5000", "PX_RH_MAX_QTY": "50",
            "PX_RH_AUDIT": str(tmp_path / "a.jsonl"), "PX_RH_HALT_FILE": str(tmp_path / "halt"),
            "PX_RH_STATE_FILE": str(tmp_path / "s.json"), **extra}


def mock_rt():
    app = dev_robinhood.make_app()
    c = Client("http://127.0.0.1/mcp", Provider(app.state.mock), transport=httpx.ASGITransport(app=app))
    c._limiter.n = 10_000
    return rh_state.Runtime(reads.Reader(c), token_present=lambda: True, market_quiet=lambda: False), app.state.mock


def test_from_env_reads_limits_and_paths(tmp_path):
    rt, _ = mock_rt()
    g = Guard.from_env(rt.reader, rt.reader.client, env_for(tmp_path))
    assert g.limits.trading and g.limits.max_qty == 50
    assert (g.audit_path, g.halt_path, g.state_path) == (tmp_path / "a.jsonl", tmp_path / "halt", tmp_path / "s.json")


def test_from_env_defaults_live_under_data(tmp_path):
    rt, _ = mock_rt()
    g = Guard.from_env(rt.reader, rt.reader.client, {})
    assert g.limits.trading is False
    assert g.audit_path.name == "rh_audit.jsonl" and g.halt_path.name == "RH_HALT" and g.state_path.name == "rh_state.json"
    assert g.audit_path.parent.name == "data" and g.audit_path.parent == g.halt_path.parent == g.state_path.parent


def test_app_builds_and_attaches_the_guard(tmp_path, monkeypatch):
    for k, v in env_for(tmp_path).items():
        monkeypatch.setenv(k, v)
    rt, _ = mock_rt()
    app = build(rh=rt)
    with TestClient(app) as c:
        assert isinstance(app.state.rh_guard, Guard) and rt.state.guard is app.state.rh_guard
        assert c.get("/api/rh/state").json()["tickets"] == []


def test_no_guard_when_rh_off_or_record_only(tmp_path, monkeypatch):
    for k, v in env_for(tmp_path).items():
        monkeypatch.setenv(k, v)
    with TestClient(build()) as c:
        assert c.app.state.rh_guard is None
    with TestClient(build(rh=mock_rt()[0], record_only=True)) as c:
        assert getattr(c.app.state, "rh_guard", None) is None


async def test_restart_restores_tickets_and_unknown_stays_halted(tmp_path):
    rt, st = mock_rt()
    env = env_for(tmp_path)
    g = Guard.from_env(rt.reader, rt.reader.client, env, market_open=lambda: True)
    st.place_reply = "drop"
    t = await g.preview({"symbol": "DDD", "side": "buy", "qty": 1, "limit_price": 100})
    assert (await g.place(t.ticket_id, confirm_hash=t.hash8))["outcome"] == "unknown"
    g2 = Guard.from_env(rt.reader, rt.reader.client, env, market_open=lambda: True)    # the restart
    assert g2.tickets[t.ticket_id].state == "unknown" and g2.halted()
    rt.attach_guard(g2)
    assert rt.state.snapshot()["tickets"][0]["state"] == "unknown" and rt.state.snapshot()["halt"] == "place_unknown"


async def test_track_loop_ticks_the_guard_and_survives_errors():
    calls = []

    class G:
        async def track(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) >= 3:
                raise asyncio.CancelledError
    holder = type("A", (), {})()
    holder.state = type("S", (), {"rh_guard": G()})()

    async def fast(_):
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await app_mod._rh_track_loop(holder, sleep=fast)
    assert len(calls) == 3
