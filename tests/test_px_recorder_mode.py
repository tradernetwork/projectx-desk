"""PX_RECORD_ONLY=1: a log-only recorder. It must be unable to touch orders, not merely unwilling."""
import secrets

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from projectx_desk import app as app_mod
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub

CID = "CON.F.US.MES.Z25"
OLD_POST_PATHS = ["/api/preview", "/api/place", "/api/flatten", "/api/stop", "/api/halt", "/api/halt/clear",
                  "/api/reconcile", "/api/intent/resolve", "/api/csrf", "/"]


class FakeClient:
    async def token(self, fresh=False):
        return "jwt"

    async def aclose(self):
        pass

    async def search_contracts(self, text, live=False):
        return []

    async def bars(self, body):
        return []


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("PX_TRADING", "PX_MCP_PLACE", "PX_RECORD_ONLY", "PX_RECORDER_URL", "PX_MCP_TOKEN", "PX_OPERATOR_TOKEN", "PX_HOST"):
        monkeypatch.delenv(k, raising=False)


def rec_app(**kw):
    fake = FakeClient()
    hub = SignalRHub(fake, "market", url="ws://127.0.0.1:1/x")
    return create_app(client=fake, market_hub=hub, allowed_hosts={"testserver"}, connect=False, record_only=True, **kw)


def test_no_write_routes_at_all():
    app = rec_app(mcp_token=secrets.token_hex(32))
    for r in app.routes:
        assert not isinstance(r, Mount), r.path               # no /mcp, no /static
        assert set(getattr(r, "methods", ()) or ()) <= {"GET", "HEAD"}, (r.path, r.methods)
    paths = {r.path for r in app.routes}
    assert {"/healthz", "/api/flow", "/api/tickbars", "/api/tickbars/config", "/api/indicators", "/api/respect"} <= paths
    assert not paths & {"/api/preview", "/api/place", "/api/flatten", "/api/stop", "/api/halt", "/api/csrf", "/state.json", "/ai/state"}


def test_every_old_post_path_is_404_not_405():
    app = rec_app()
    with TestClient(app) as c:
        for p in OLD_POST_PATHS[:-2] + ["/mcp", "/mcp/", "/api/flow"]:
            if p == "/api/flow":
                continue                                     # a GET route: a POST there is 405, and it only reads
            r = c.post(p, json={}, headers={"origin": "http://testserver", "content-type": "application/json"})
            assert r.status_code == 404, (p, r.status_code)
        for m in ("put", "delete", "patch"):
            assert getattr(c, m)("/api/place", headers={"origin": "http://testserver"}).status_code == 404


def test_no_guard_no_user_hub_no_mcp_only_the_market_hub(monkeypatch):
    made = []
    real = app_mod.SignalRHub

    def hub(client, name, *a, **k):
        made.append(name)
        return real(client, name, url="ws://127.0.0.1:1/x")

    def boom(*a, **k):
        raise AssertionError("a recorder must not construct a Guard")
    monkeypatch.setattr(app_mod, "SignalRHub", hub)
    monkeypatch.setattr(app_mod, "Guard", boom)
    app = create_app(client=FakeClient(), allowed_hosts={"testserver"}, connect=False, record_only=True, mcp_token=secrets.token_hex(32))
    with TestClient(app) as c:
        assert made == ["market"]
        for attr in ("guard", "user", "mcp", "csrf"):
            assert not hasattr(app.state, attr), attr
        assert c.get("/mcp/").status_code == 404
        assert c.get("/healthz").text == "ok"


def test_status_page_and_read_routes_work():
    app = rec_app()
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "RECORDER" in r.text and "no order path" in r.text
        t = {"timestamp": "2025-01-02T15:00:05+00:00", "price": 100.0, "volume": 3, "type": 0}
        app.state.live._on_trade(CID, [t])
        f = c.get("/api/flow", params={"contract": CID}).json()
        assert f["source"] == "local" and f["since"] is not None
        assert c.get("/api/tickbars/config").json()["sizes"]
        n = c.get("/api/tickbars/config").json()["sizes"][0]
        assert c.get("/api/tickbars", params={"contract": CID, "n": n}).json()["source"] == "local"


def test_boot_line(capsys):
    rec_app()
    assert "RECORD ONLY: no order path" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["PX_TRADING", "PX_MCP_PLACE"])
@pytest.mark.parametrize("val", ["1", "true", "YES"])
def test_refuses_to_start_with_trading_on(monkeypatch, key, val):
    monkeypatch.setenv(key, val)
    with pytest.raises(SystemExit) as e:
        rec_app()
    assert key in str(e.value) and f"{key}={val}" not in str(e.value)       # the name is printed, never the value
    monkeypatch.setenv("PX_RECORD_ONLY", "yes")                      # the env switch alone reaches the same refusal
    with pytest.raises(SystemExit):
        create_app(client=FakeClient(), allowed_hosts={"testserver"}, connect=False)


def test_main_refuses_before_taking_the_lock(monkeypatch, tmp_path):
    lock = tmp_path / "x.lock"
    monkeypatch.setenv("PX_NO_ENV_FILE", "1")
    monkeypatch.setenv("PX_LOCK_FILE", str(lock))
    monkeypatch.setenv("PX_RECORD_ONLY", "1")
    monkeypatch.setenv("PX_TRADING", "1")
    with pytest.raises(SystemExit):
        app_mod.main()
    assert not lock.exists()


def test_recorder_mode_keeps_the_offloopback_rules(monkeypatch):
    monkeypatch.setenv("PX_HOST", "0.0.0.0")
    with pytest.raises(SystemExit):
        rec_app()                                                    # no PX_ALLOWED_HOSTS / PX_OPERATOR_TOKEN


def test_mode_off_by_default_keeps_the_desk(monkeypatch):
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                     user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False)
    assert any("POST" in (getattr(r, "methods", None) or ()) for r in app.routes)
    for v in ("0", "", "no"):
        monkeypatch.setenv("PX_RECORD_ONLY", v)
        assert app_mod.record_only_enabled() is False
