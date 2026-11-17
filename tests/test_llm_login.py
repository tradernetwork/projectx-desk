import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx

from projectx_desk import llm_config as lc
from projectx_desk import llm_login as ll

MINTED = "sk-or-MINTEDKEY1234567890"
EX = "https://fake.test/api/v1/auth/keys"


def mk(tmp_path, handler=None, expiry=30.0, opener=None):
    seen = {}

    def default(req):
        seen["body"], seen["url"] = json.loads(req.content), str(req.url)
        return httpx.Response(200, json={"key": MINTED})

    path = tmp_path / "llm.json"
    lg = ll.OrLogin(ll.LoginCfg(exchange_url=EX, expiry_s=expiry), path, {}, httpx.MockTransport(handler or default), opener)
    return lg, path, seen


async def hit(port, target):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {target} HTTP/1.1\r\nhost: localhost\r\n\r\n".encode())
    await w.drain()
    data = await r.read(4096)
    w.close()
    return data.decode()


def parts(url):
    u = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    return u, q, int(urlsplit(q["callback_url"]).port)


async def settle(lg, pid, want, tries=100):
    for _ in range(tries):
        if lg.status(pid) == want:
            return True
        await asyncio.sleep(0.02)
    return False


def test_pkce_pair_s256():
    v, c = ll.pkce_pair()
    assert len(v) == 43 and "=" not in v + c
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert ll.pkce_pair()[0] != v


def test_allowed_urls():
    assert ll.is_allowed_openrouter_url("https://openrouter.ai/auth?x=1")
    for bad in ("http://openrouter.ai/auth", "https://openrouter.ai.evil.com/auth", "https://evil.com/openrouter.ai",
                "https://user@openrouter.ai/auth", "javascript:alert(1)", "file:///etc/passwd", ""):
        assert not ll.is_allowed_openrouter_url(bad)


async def test_full_flow(tmp_path):
    opened = []
    lg, path, seen = mk(tmp_path, opener=opened.append)
    s = await lg.start()
    u, q, port = parts(s["url"])
    assert s["url"].startswith("https://openrouter.ai/auth?") and ll.is_allowed_openrouter_url(s["url"])
    assert opened == [s["url"]]
    assert q["code_challenge_method"] == "S256" and q["callback_url"].startswith("http://localhost:") and q["callback_url"].endswith("/callback")
    assert lg.status(s["pending_id"]) == "pending"
    page = await hit(port, f"/callback?code=thecode&state={q['state']}")
    assert "200 OK" in page and MINTED not in page and "thecode" not in page
    assert await settle(lg, s["pending_id"], "done")
    # exchange used the verifier matching the challenge
    v = seen["body"]["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert seen["body"]["code"] == "thecode" and seen["body"]["code_challenge_method"] == "S256" and seen["url"] == EX
    # key saved sealed, exactly like a pasted one, never returned
    r = lc.resolve(path, {})
    assert r.key == MINTED and r.base_url == "https://openrouter.ai/api/v1"
    for obj in (s, lg.status(s["pending_id"]), lc.view(path, {}), repr(lg)):
        assert MINTED not in repr(obj)
    for obj in (lg.status(s["pending_id"]), lc.view(path, {}), repr(lg)):
        assert q["state"] not in repr(obj)
    # port released
    try:
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
        assert False, "listener still open"
    except OSError:
        pass


async def test_wrong_state_ignored_then_good_one_works(tmp_path):
    lg, path, _ = mk(tmp_path)
    s = await lg.start()
    _, q, port = parts(s["url"])
    assert "400" in await hit(port, "/callback?code=x&state=WRONG")
    assert "400" in await hit(port, "/callback?code=x")
    assert "404" in await hit(port, "/other")
    assert lg.status(s["pending_id"]) == "pending"
    await hit(port, f"/callback?code=ok&state={q['state']}")
    assert await settle(lg, s["pending_id"], "done")


async def test_denied_is_failed(tmp_path):
    lg, path, _ = mk(tmp_path)
    s = await lg.start()
    _, _, port = parts(s["url"])
    await hit(port, "/callback?error=access_denied")
    assert await settle(lg, s["pending_id"], "failed")
    assert lc.resolve(path, {}) is None


async def test_one_pending_at_a_time_then_again_after_done(tmp_path):
    lg, _, _ = mk(tmp_path)
    s = await lg.start()
    try:
        await lg.start()
        assert False
    except ll.LoginError as e:
        assert e.code == "rate_limited"
    _, q, port = parts(s["url"])
    await hit(port, f"/callback?code=c&state={q['state']}")
    assert await settle(lg, s["pending_id"], "done")
    s2 = await lg.start()
    assert s2["pending_id"] != s["pending_id"]
    assert lg.status(s["pending_id"]) == "failed"         # an old id is not the current login
    await lg.cancel()


async def test_expiry(tmp_path):
    lg, path, _ = mk(tmp_path, expiry=0.2)
    s = await lg.start()
    _, _, port = parts(s["url"])
    assert await settle(lg, s["pending_id"], "expired")
    await asyncio.sleep(0.1)
    try:
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
        assert False
    except OSError:
        pass
    assert lg.status("bogus") == "failed" and lg.status(None) == "failed"
    await lg.start()        # an expired login does not block a new one
    await lg.cancel()


async def test_exchange_failures_are_failed(tmp_path):
    cases = [lambda r: httpx.Response(401, json={"key": MINTED}), lambda r: httpx.Response(200, json={"key": "not-an-or-key"}),
             lambda r: httpx.Response(200, text="junk"), lambda r: httpx.Response(200, json={}),
             lambda r: httpx.Response(302, headers={"location": "https://evil.example"}),
             lambda r: httpx.Response(200, content=b"{" + b" " * 20000 + b"}"),
             lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))]
    for h in cases:
        lg, path, _ = mk(tmp_path, handler=h)
        s = await lg.start()
        _, q, port = parts(s["url"])
        await hit(port, f"/callback?code=c&state={q['state']}")
        assert await settle(lg, s["pending_id"], "failed")
        assert lc.resolve(path, {}) is None


async def test_keeps_model_when_already_openrouter(tmp_path):
    lg, path, _ = mk(tmp_path)
    lc.save({"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}, path, {})
    s = await lg.start()
    _, q, port = parts(s["url"])
    await hit(port, f"/callback?code=c&state={q['state']}")
    assert await settle(lg, s["pending_id"], "done")
    assert lc.resolve(path, {}).model == "deepseek/deepseek-v4.1-flash"


async def test_opener_failure_and_foreign_url_not_opened(tmp_path):
    def bad(url):
        raise RuntimeError("no browser")

    lg, _, _ = mk(tmp_path, opener=bad)
    s = await lg.start()
    assert s["url"]
    await lg.cancel()
    opened = []
    lg2 = ll.OrLogin(ll.LoginCfg(auth_url="https://evil.example/auth", exchange_url=EX), tmp_path / "x.json", {}, None, opened.append)
    await lg2.start()
    assert opened == []          # not an openrouter.ai URL: never handed to a browser
    await lg2.cancel()
