"""Phase 0, task 3: the read-only Robinhood routes, the digest lines and the in-process mock."""
import sys
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from projectx_desk import app as app_mod
from projectx_desk import dev_robinhood
from projectx_desk.app import create_app
from projectx_desk.digest import format_digest, rh_lines
from projectx_desk.hub import SignalRHub
from projectx_desk.rh import reads, state as rh_state
from projectx_desk.rh.mcp import Client
from tests.test_px_digest import state as digest_state
from tests.test_rh_reads import CountingTransport, MCP_ACCT, OTHER_ACCT, Provider

ROUTES = ["/api/rh/state", "/api/rh/positions", "/api/rh/orders", "/api/rh/approvals"]


class FakeClient:
    async def token(self, fresh=False):
        return "jwt"

    async def aclose(self):
        pass

    async def search_contracts(self, text, live=False):
        return []

    async def bars(self, body):
        return []


def build(rh=None, record_only=False, **kw):
    fake = FakeClient()
    hub = SignalRHub(fake, "market", url="ws://127.0.0.1:1/x")
    uh = None if record_only else SignalRHub(fake, "user", url="ws://127.0.0.1:1/y")
    return create_app(client=fake, market_hub=hub, user_hub=uh, allowed_hosts={"testserver"}, connect=False, rh=rh,
                      record_only=record_only, **kw)


def mock_runtime(quiet=False):
    app = dev_robinhood.make_app()
    c = Client("http://127.0.0.1/mcp", Provider(app.state.mock), transport=httpx.ASGITransport(app=app))
    return rh_state.Runtime(reads.Reader(c), token_present=lambda: True, market_quiet=lambda: quiet)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("PX_RH", "PX_RH_MOCK", "PX_RECORD_ONLY", "PX_TRADING", "PX_MCP_PLACE", "PX_MCP_TOKEN", "PX_OPERATOR_TOKEN", "PX_HOST"):
        monkeypatch.delenv(k, raising=False)


def test_routes_disabled_without_runtime():
    with TestClient(build()) as c:
        for p in ROUTES:
            r = c.get(p)
            assert r.status_code == 200 and r.json() == {"enabled": False}


def test_routes_serve_mcp_account_only():
    rt = mock_runtime()
    app = build(rh=rt)
    with TestClient(app) as c:
        for _ in range(100):                                    # the lifespan started the poll task; wait for the first snapshot
            if c.get("/api/rh/state").json().get("as_of"):
                break
            time.sleep(0.05)
        bodies = {p: c.get(p) for p in ROUTES}
        for p, r in bodies.items():
            assert r.status_code == 200, p
            assert OTHER_ACCT not in r.text and MCP_ACCT not in r.text and "personal" not in r.text
        assert [x["symbol"] for x in bodies["/api/rh/positions"].json()["positions"]][:3] == ["AAA", "BBB", "CCC"]
        assert bodies["/api/rh/orders"].json()["orders"][0]["id"] == "mock-eo-1"
        assert bodies["/api/rh/approvals"].json()["approvals"][0]["status"] == "pending"
        assert bodies["/api/rh/state"].json()["ok"] is True
        assert c.post("/api/rh/state", json={}).status_code in (403, 404, 405)


def test_rh_post_routes_are_exactly_the_phase_1_order_routes():
    app = build(rh=mock_runtime())
    posts = {r.path for r in app.routes if "/rh" in r.path and "POST" in r.methods}
    assert posts == {"/api/rh/preview", "/api/rh/place", "/api/rh/cancel", "/api/rh/release", "/api/rh/reconcile", "/api/rh/halt/resolve"}
    for r in app.routes:
        if "/rh" in r.path and r.path not in posts:
            assert set(r.methods) <= {"GET", "HEAD"}, r.path


def test_record_only_has_no_rh_routes():
    app = build(rh=mock_runtime(), record_only=True)
    assert not [r.path for r in app.routes if "/rh" in r.path]


def test_pxrh_off_makes_no_outbound_call(monkeypatch):
    t = CountingTransport()
    monkeypatch.setattr(rh_state, "_transport_for_tests", t, raising=False)
    monkeypatch.setenv("PX_RH", "0")
    with TestClient(build()) as c:
        assert c.get("/api/rh/state").json() == {"enabled": False}
    assert t.n == 0


def test_digest_rh_lines_and_leaks():
    rt = mock_runtime()
    asyncio.run(rt.poll_once())
    s = digest_state(rh=rt.state.ai_block(stream=False))
    t = format_digest(s)
    assert "RH: ENABLED connected=YES ok=YES" in t and "RH_POS: n=5 AAA long qty=10" in t and "approvals=1" in t
    assert OTHER_ACCT not in t and MCP_ACCT not in t


def test_digest_unknown_and_stream_and_absent():
    assert "RH:" not in format_digest(digest_state())
    assert "RH:" not in format_digest(digest_state(rh={"enabled": False}))
    assert "RH: ENABLED connected=NO" in format_digest(digest_state(rh={"enabled": True, "connected": False}))
    unk = format_digest(digest_state(rh={"enabled": True, "connected": True, "ok": False, "unknown": ["account"],
                                         "positions": None, "orders": None, "approvals": None}))
    assert "RH_POS: UNKNOWN" in unk and "RH_POS: NONE" not in unk
    rt = mock_runtime()
    asyncio.run(rt.poll_once())
    strm = "\n".join(l for l in format_digest(digest_state(rh=rt.state.ai_block(stream=True))).splitlines() if l.startswith("RH"))
    assert "avg=" not in strm and "px=" not in strm and "realized" not in strm and "RH_POS:" in strm
    full = "\n".join(l for l in format_digest(digest_state(rh=rt.state.ai_block(stream=False))).splitlines() if l.startswith("RH"))
    assert "avg=100.5" in full and "realized=12.5" in full


def test_digest_length_cap():
    big = {"enabled": True, "connected": True, "ok": True, "unknown": [], "stream": False,
           "positions": [{"symbol": f"S{i}", "asset": "equity", "qty": 1.0, "avg_price": 1.0, "side": "long", "unknown": []}
                         for i in range(200)],
           "orders": [], "approvals": [], "approval_setting": {"human_must_approve": True}, "pnl": None}
    lines = [l for l in rh_lines(big)]
    assert len(lines) <= 8 and all(len(l) <= 400 for l in lines)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes and exec bits")
async def test_dev_mock_in_process_and_tokens(tmp_path):
    from projectx_desk.rh.tokens import TokenStore
    f = tmp_path / "mock_tokens.json"
    url, stop = await dev_robinhood.start_in_process(f)
    try:
        assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp") and f.exists()
        assert (f.stat().st_mode & 0o777) == 0o600
        store = TokenStore(f, base_url=url)
        c = Client(url, store)
        accts = await reads.Reader(c).positions()
        assert accts[0]["symbol"] == "AAA"
    finally:
        await stop()
