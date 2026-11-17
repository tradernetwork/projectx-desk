"""Phase 0, task 2: OAuth PKCE login and the rotating token store, end to end against the mock."""
import asyncio
import json
import logging
import os
import stat
import urllib.parse

import httpx
import pytest

from projectx_desk import dev_robinhood
from projectx_desk.rh import oauth
from projectx_desk.rh.mcp import Client, McpError
from projectx_desk.rh.tokens import TokenStore, Tokens

BASE = "http://127.0.0.1/mcp"


class Counting(httpx.AsyncBaseTransport):
    """Wraps the ASGI mock and counts refresh-token grants."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.refresh_posts = 0

    async def handle_async_request(self, request):
        if request.url.path == "/token" and b"grant_type=refresh_token" in request.content:
            self.refresh_posts += 1
        return await self.inner.handle_async_request(request)


def rig(tmp_path, **kw):
    app = dev_robinhood.make_app()
    tr = Counting(app)
    clock = [1000.0]
    store = TokenStore(tmp_path / "rh_tokens.json", base_url=BASE, transport=tr, now=lambda: clock[0])
    return app, app.state.mock, tr, clock, store


async def browse(app, url, mutate=None):
    """Plays the browser: hit /authorize on the mock, follow its redirect to the loopback listener for real."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as h:
        r = await h.get(url)
    loc = r.headers["location"].replace("//localhost:", "//127.0.0.1:")
    if mutate:
        loc = mutate(loc)
    async with httpx.AsyncClient() as real:
        await real.get(loc)


async def settle(login, pid, want=("done", "failed", "expired")):
    for _ in range(300):
        if login.status(pid) in want:
            return login.status(pid)
        await asyncio.sleep(0.01)
    return login.status(pid)


def seeded(st, store, clock, ttl=10):
    t = st.issue("c")
    store.save(Tokens(t["access_token"], t["refresh_token"], clock[0] + ttl, "c"))
    return t


async def test_full_login_end_to_end_then_the_client_uses_the_stored_token(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    tasks = []
    login = oauth.Login(BASE, store, opener=lambda u: tasks.append(asyncio.create_task(browse(app, u))), transport=tr,
                        now=lambda: clock[0])
    out = await login.start()
    assert login.status(out["pending_id"]) in ("pending", "done")
    assert await settle(login, out["pending_id"]) == "done"
    await asyncio.gather(*tasks)
    t = store.load()
    assert t and t.client_id.startswith("mock-client-")
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    c = Client(BASE, store, transport=httpx.ASGITransport(app=app))
    assert "accounts" in await c.call("get_accounts")
    await login.aclose()


async def test_wrong_state_fails_and_saves_nothing(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    tasks = []

    def tamper(loc):
        return loc.replace("state=", "state=x")

    login = oauth.Login(BASE, store, transport=tr, now=lambda: clock[0],
                        opener=lambda u: tasks.append(asyncio.create_task(browse(app, u, tamper))))
    out = await login.start()
    assert await settle(login, out["pending_id"]) == "failed"
    await asyncio.gather(*tasks)
    assert store.load() is None and not store.path.exists()
    await login.aclose()


async def test_callback_with_a_foreign_host_header_is_ignored(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    seen = {}

    def opener(url):
        seen["url"] = url

    login = oauth.Login(BASE, store, transport=tr, now=lambda: clock[0], opener=opener)
    out = await login.start()
    port = login._logins[out["pending_id"]]["server"].sockets[0].getsockname()[1]
    async with httpx.AsyncClient() as real:
        r = await real.get(f"http://127.0.0.1:{port}/callback?code=x&state=y", headers={"Host": "evil.test"})
    assert r.status_code == 404 and login.status(out["pending_id"]) == "pending"
    await login.aclose()


async def test_second_concurrent_login_is_rate_limited_then_allowed_after_expiry(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    login = oauth.Login(BASE, store, transport=tr, now=lambda: clock[0])
    first = await login.start()
    with pytest.raises(McpError) as e:
        await login.start()
    assert e.value.code == "rate_limited"
    clock[0] += 601
    assert login.status(first["pending_id"]) == "expired"
    second = await login.start()                                       # the expired one no longer blocks
    assert second["pending_id"] != first["pending_id"]
    await login.aclose()


async def test_opener_only_sees_allowed_urls():
    rh = "https://agent.robinhood.com/mcp/trading"
    assert oauth.allowed_auth_url("https://robinhood.com/oauth?x=1", rh)
    assert oauth.allowed_auth_url("https://api.robinhood.com/oauth2/token/", rh)
    assert not oauth.allowed_auth_url("http://robinhood.com/oauth", rh)
    assert not oauth.allowed_auth_url("https://robinhood.com.evil.test/oauth", rh)
    assert not oauth.allowed_auth_url("https://evil.test/oauth", rh)
    assert oauth.allowed_auth_url("http://127.0.0.1/authorize", BASE)
    assert not oauth.allowed_auth_url("http://127.0.0.1:9999/authorize", BASE)
    assert not oauth.allowed_auth_url("https://robinhood.com/oauth", BASE)


async def test_metadata_pointing_at_a_foreign_host_is_refused(tmp_path):
    def handler(request):
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json={"authorization_endpoint": "https://evil.test/a",
                                             "token_endpoint": "https://evil.test/t",
                                             "registration_endpoint": "https://evil.test/r"})
        return httpx.Response(500)

    opened = []
    store = TokenStore(tmp_path / "t.json")
    login = oauth.Login("https://agent.robinhood.com/mcp/trading", store, opener=opened.append,
                        transport=httpx.MockTransport(handler))
    with pytest.raises(McpError):
        await login.start()
    assert opened == []


async def test_refresh_rotates_and_the_old_token_is_dead(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    old = seeded(st, store, clock)
    new_access = await store.access_token()                           # inside the 60 s skew: refreshes
    assert new_access != old["access_token"] and tr.refresh_posts == 1
    t = store.load()
    assert t.access == new_access and t.refresh != old["refresh_token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as h:
        r = await h.post("/token", data={"grant_type": "refresh_token", "refresh_token": old["refresh_token"],
                                          "client_id": "c"})
    assert r.status_code == 400                                       # single use: the spent token is dead


async def test_fresh_token_is_not_refreshed(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    old = seeded(st, store, clock, ttl=3600)
    assert await store.access_token() == old["access_token"] and tr.refresh_posts == 0


async def test_save_failure_reads_as_read_down_and_the_spent_token_is_never_reused(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    old = seeded(st, store, clock)
    real_save, fail = store.save, [True]

    def flaky(tokens):
        if fail[0]:
            raise OSError("disk full")
        return real_save(tokens)

    store.save = flaky
    with pytest.raises(McpError) as e:
        await store.access_token()
    assert e.value.code == "unauthorized"                              # read-down, not a crash
    assert tr.refresh_posts == 1
    assert store.load().refresh == old["refresh_token"]               # the old FILE is intact (atomic)
    with pytest.raises(McpError):                                      # still failing: no second refresh spends the dead token
        await store.access_token()
    with pytest.raises(McpError):
        await store.force_refresh()
    assert tr.refresh_posts == 1
    fail[0] = False                                                    # disk recovers: the in-memory NEW pair is persisted
    tok = await store.access_token()
    assert tr.refresh_posts == 1 and store.load().access == tok and store.load().refresh != old["refresh_token"]


async def test_rejected_refresh_is_unauthorized_and_leaves_the_file(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    store.save(Tokens("a", "dead-refresh", clock[0] + 1, "c"))
    before = store.path.read_text()
    with pytest.raises(McpError) as e:
        await store.access_token()
    assert e.value.code == "unauthorized" and store.path.read_text() == before


async def test_missing_or_garbled_file_is_unauthorized(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    with pytest.raises(McpError) as e:
        await store.access_token()
    assert e.value.code == "unauthorized"
    for junk in ("", "{not json", "[]", json.dumps({"access": "a"}),
                 json.dumps({"access": "a", "refresh": "r", "client_id": "c", "expires_at": "soon"}),
                 json.dumps({"access": "a", "refresh": "r", "client_id": "c", "expires_at": float("nan")})):
        store.path.write_text(junk)
        s2 = TokenStore(store.path, base_url=BASE, transport=tr, now=lambda: clock[0])
        assert s2.load() is None
        with pytest.raises(McpError):
            await s2.access_token()


async def test_no_refresh_without_a_base_url(tmp_path):
    s = TokenStore(tmp_path / "t.json", now=lambda: 1000.0)
    s.save(Tokens("a", "r", 1001.0, "c"))
    with pytest.raises(McpError):
        await s.access_token()


async def test_file_mode_is_0600_even_over_a_loose_umask_and_no_temp_left(tmp_path):
    old = os.umask(0o000)
    try:
        s = TokenStore(tmp_path / "sub" / "t.json")
        s.save(Tokens("a", "r", 1.0, "c"))
        s.save(Tokens("a2", "r2", 2.0, "c"))
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(s.path).st_mode) == 0o600
    assert [p.name for p in s.path.parent.iterdir()] == ["t.json"]


async def test_secrets_never_appear_in_repr_str_errors_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, st, tr, clock, store = rig(tmp_path)
    old = seeded(st, store, clock)
    secrets_ = [old["access_token"], old["refresh_token"]]
    t = store.load()
    assert all(s not in repr(t) and s not in str(t) for s in secrets_)
    assert all(s not in repr(store) for s in secrets_)
    new = await store.access_token()
    secrets_.append(new)
    s2 = TokenStore(tmp_path / "bad.json", base_url=BASE, transport=tr, now=lambda: clock[0])
    s2.save(Tokens("A-SECRET", "R-SECRET", clock[0] + 1, "c"))
    with pytest.raises(McpError) as e:
        await s2.access_token()
    assert "SECRET" not in str(e.value) and "SECRET" not in repr(e.value)
    blob = caplog.text + repr(t)
    assert all(s not in blob for s in secrets_ + ["A-SECRET", "R-SECRET"])


async def test_401_from_the_server_triggers_force_refresh_on_the_store(tmp_path):
    app, st, tr, clock, store = rig(tmp_path)
    t = st.issue("c")
    store.save(Tokens(t["access_token"], t["refresh_token"], clock[0] + 3600, "c"))
    st.access[t["access_token"]] = 0                                   # the server dropped it early
    c = Client(BASE, store, transport=httpx.ASGITransport(app=app))
    assert "accounts" in await c.call("get_accounts")
    assert tr.refresh_posts == 1
