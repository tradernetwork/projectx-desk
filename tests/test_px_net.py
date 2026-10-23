import base64

import pytest
from fastapi.testclient import TestClient

from projectx_desk import net as px_net
from projectx_desk.app import create_app
from projectx_desk.mcp import weak_token_reason
from test_px_app import FakeClient
from test_px_mcp import GOOD as MCP_TOKEN
from test_px_guard_place import build
from projectx_desk.hub import SignalRHub

OP = "c3a9f1e07b5d4a28916e0d3b7f4c8a52e1d09b6f3a7c4e85d2b1f0a9c8e7d6b5"   # a strong operator token for tests


def basic(token, user="anyone"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}


def test_loopback_detection():
    for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2", "LOCALHOST"):
        assert px_net.is_loopback(h), h
    for h in ("0.0.0.0", "192.168.1.5", "desk.lan", "", "::", "10.0.0.1"):
        assert not px_net.is_loopback(h), h


def test_host_and_origin_allowlists_always_include_loopback_and_both_schemes():
    hosts = px_net.allowed_hosts(8600, "desk.lan, 10.0.0.5:9000")
    assert {"127.0.0.1:8600", "localhost:8600", "desk.lan", "desk.lan:8600", "10.0.0.5:9000"} <= hosts
    assert "evil.com" not in hosts and "10.0.0.5" not in hosts           # a port was given, so only that form
    origins = px_net.allowed_origins(hosts, "https://proxy.example")
    assert {"http://desk.lan", "https://desk.lan", "http://127.0.0.1:8600", "https://proxy.example"} <= origins
    assert "http://evil.com" not in origins


def test_bind_problem_is_none_on_loopback_and_demands_hosts_and_a_strong_token_elsewhere():
    assert px_net.bind_problem("127.0.0.1", None, None, weak_token_reason) is None
    assert px_net.bind_problem("localhost", None, None, weak_token_reason) is None
    assert "PX_ALLOWED_HOSTS" in px_net.bind_problem("0.0.0.0", OP, "", weak_token_reason)
    assert "PX_OPERATOR_TOKEN" in px_net.bind_problem("0.0.0.0", None, "desk.lan", weak_token_reason)
    assert "PX_OPERATOR_TOKEN" in px_net.bind_problem("0.0.0.0", "short", "desk.lan", weak_token_reason)
    assert "PX_OPERATOR_TOKEN" in px_net.bind_problem("0.0.0.0", "your_strong_random_secret_token_xxxxxxxxxxxx", "desk.lan", weak_token_reason)
    assert px_net.bind_problem("0.0.0.0", OP, "desk.lan", weak_token_reason) is None
    msg = px_net.bind_problem("0.0.0.0", "short-secret-xyz", "desk.lan", weak_token_reason)
    assert "short-secret-xyz" not in msg                                  # never quotes the token


def test_operator_ok_is_basic_any_username_exact_password():
    assert px_net.operator_ok(basic(OP)["Authorization"], OP)
    assert px_net.operator_ok(basic(OP, user="")["Authorization"], OP)
    assert not px_net.operator_ok(basic(OP[:-1] + "0")["Authorization"], OP)
    assert not px_net.operator_ok(None, OP) and not px_net.operator_ok("", OP) and not px_net.operator_ok("Bearer " + OP, OP)
    assert not px_net.operator_ok("Basic !!!notbase64", OP) and not px_net.operator_ok("Basic ", OP)
    assert not px_net.operator_ok(basic(OP)["Authorization"], None)       # no configured token: never a match


def test_which_paths_need_the_login():
    assert px_net.needs_operator_login("/") and px_net.needs_operator_login("/api/place") and px_net.needs_operator_login("/ai/digest")
    assert not px_net.needs_operator_login("/healthz") and not px_net.needs_operator_login("/mcp/") and not px_net.needs_operator_login("/mcp")
    assert px_net.needs_operator_login("/mcpx") and px_net.needs_operator_login("/healthz/extra")


def make(tmp_path, **kw):
    g, live, clock, _ = build(tmp_path)
    fake = FakeClient()
    return create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                      user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"},
                      connect=False, guard=g, **kw)


def test_with_an_operator_token_every_route_needs_the_login_except_healthz_and_mcp(tmp_path):
    app = make(tmp_path, operator_token=OP, mcp_token=MCP_TOKEN)
    with TestClient(app) as c:
        for path in ("/", "/state.json", "/api/csrf", "/ai/digest", "/ai/state", "/llms.txt", "/contracts", "/static/desk.js"):
            r = c.get(path)
            assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], path
        assert c.post("/api/halt", json={}).status_code == 401              # the POST routes are locked too
        assert c.get("/state.json", headers=basic("wrong" * 10)).status_code == 401
        assert c.get("/state.json", headers=basic(OP)).status_code == 200
        assert c.get("/", headers=basic(OP)).status_code == 200
        hz = c.get("/healthz")
        assert hz.status_code == 200 and hz.text == "ok"                    # open, and says nothing else


def test_mcp_keeps_its_own_bearer_and_is_not_gated_by_the_operator_login(tmp_path):
    app = make(tmp_path, operator_token=OP, mcp_token=MCP_TOKEN)
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with TestClient(app) as c:
        assert c.post("/mcp/", json=body, headers=hdr).status_code == 401
        assert c.post("/mcp/", json=body, headers={**hdr, "authorization": "Bearer " + MCP_TOKEN}).status_code == 200
        assert c.post("/mcp/", json=body, headers={**hdr, **basic(OP)}).status_code == 401      # the operator token is not an MCP token


def test_a_bad_host_is_rejected_before_the_login_is_even_considered(tmp_path):
    app = make(tmp_path, operator_token=OP)
    with TestClient(app) as c:
        r = c.get("/state.json", headers={**basic(OP), "host": "evil.com"})
        assert r.status_code == 403 and "WWW-Authenticate" not in r.headers


def test_a_weak_operator_token_stops_the_app_from_building(tmp_path):
    for bad in ("tiny-q7", "your_strong_random_secret_token_xxxxxxxxxxxx", "a" * 40):
        with pytest.raises(SystemExit) as e:
            make(tmp_path, operator_token=bad)
        assert bad not in str(e.value)


def test_posts_work_through_the_login_and_https_origins_come_from_the_allowlist(tmp_path):
    app = make(tmp_path, operator_token=OP, allowed_origins={"http://testserver", "https://desk.example.com"})
    with TestClient(app) as c:
        tok = c.get("/api/csrf", headers=basic(OP)).json()["token"]
        h = {**basic(OP), "origin": "https://desk.example.com", "x-px-csrf": tok, "content-type": "application/json"}
        assert c.post("/api/halt", json={}, headers=h).status_code == 200
        assert c.post("/api/halt", json={}, headers={**h, "origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/halt", json={}, headers={k: v for k, v in h.items() if k != "x-px-csrf"}).status_code == 403


def test_main_refuses_an_unsafe_bind_before_starting_a_server(monkeypatch):
    from projectx_desk import app as px_app
    called = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.append((a, k)))
    for k in ("PX_OPERATOR_TOKEN", "PX_ALLOWED_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PX_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as e:
        px_app.main()
    assert "refusing to bind" in str(e.value) and called == []
    monkeypatch.setenv("PX_ALLOWED_HOSTS", "desk.lan")
    with pytest.raises(SystemExit):
        px_app.main()                                                          # hosts alone are not enough
    monkeypatch.setenv("PX_OPERATOR_TOKEN", OP)
    px_app.main()                                                              # both present: it starts (uvicorn is stubbed)
    assert called and called[0][1]["host"] == "0.0.0.0"


def test_loopback_default_still_starts_with_no_extra_settings(monkeypatch):
    from projectx_desk import app as px_app
    called = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: called.append(k))
    for k in ("PX_HOST", "PX_OPERATOR_TOKEN", "PX_ALLOWED_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    px_app.main()
    assert called and called[0]["host"] == "127.0.0.1"


def test_open_lan_is_an_explicit_opt_in_that_still_needs_hosts_and_never_accepts_a_weak_token():
    assert "PX_OPERATOR_TOKEN" in px_net.bind_problem("0.0.0.0", None, "desk.lan", weak_token_reason)               # default: refuses
    assert px_net.bind_problem("0.0.0.0", None, "desk.lan", weak_token_reason, open_lan=True) is None
    assert px_net.bind_problem("0.0.0.0", "", "desk.lan", weak_token_reason, open_lan=True) is None
    assert "PX_ALLOWED_HOSTS" in px_net.bind_problem("0.0.0.0", None, "", weak_token_reason, open_lan=True)           # rebinding guard stays
    assert "PX_OPERATOR_TOKEN" in px_net.bind_problem("0.0.0.0", "short", "desk.lan", weak_token_reason, open_lan=True)


def test_open_lan_flag_reads_only_exactly_one(monkeypatch):
    for v, want in (("1", True), (" 1 ", True), ("0", False), ("", False), ("true", False)):
        monkeypatch.setenv("PX_OPEN_LAN", v)
        assert px_net.open_lan_enabled() is want
