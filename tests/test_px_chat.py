"""Desk chat: loopback-only config, the shared POST gate, fixed error codes, and a static pin that the box can only talk to /api/chat."""
import pathlib
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from projectx_desk import chat
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub
from test_px_app import FakeClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "projectx_desk" / "static"
TOKEN = "a" * 64


def agent(handler):
    return httpx.MockTransport(handler)


def app_with(transport, cfg=("http://127.0.0.1:9315", TOKEN)):
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                     user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False)
    app.state.chat = chat.Chat(cfg, transport)
    return app


def hpost(c, tok, body, **hdr):
    h = {"origin": "http://testserver", "x-px-csrf": tok, "content-type": "application/json"}
    h.update(hdr)
    return c.post("/api/chat", json=body, headers={k: v for k, v in h.items() if v is not None})


# ---- config -------------------------------------------------------------------------------------------------------------

def test_off_unless_a_loopback_url_and_a_real_token_are_set(tmp_path):
    assert chat.config({}) is None
    assert chat.config({"PX_CHAT_URL": "http://127.0.0.1:9315", "PX_CHAT_TOKEN": TOKEN}) == ("http://127.0.0.1:9315", TOKEN)
    for bad in ("http://192.168.2.172:9315", "https://127.0.0.1:9315", "http://evil.com", "ftp://127.0.0.1"):
        assert chat.config({"PX_CHAT_URL": bad, "PX_CHAT_TOKEN": TOKEN}) is None, bad
    assert chat.config({"PX_CHAT_URL": "http://127.0.0.1:9315", "PX_CHAT_TOKEN": "short"}) is None
    f = tmp_path / "tok"
    f.write_text(TOKEN + "\n")
    assert chat.config({"PX_CHAT_URL": "http://localhost:9315", "PX_CHAT_TOKEN_FILE": str(f)}) == ("http://localhost:9315", TOKEN)
    assert chat.config({"PX_CHAT_URL": "http://localhost:9315", "PX_CHAT_TOKEN_FILE": str(tmp_path / "missing")}) is None


# ---- routes -------------------------------------------------------------------------------------------------------------

def test_post_uses_the_same_gate_as_every_desk_post():
    app = app_with(agent(lambda r: httpx.Response(200, json={"result": "x"})))
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        body = {"prompt": "hi"}
        assert hpost(c, tok, body, origin=None).status_code == 403
        assert hpost(c, tok, body, origin="http://evil.com").status_code == 403
        assert hpost(c, tok, body, **{"x-px-csrf": None}).status_code == 403
        assert hpost(c, tok, body, **{"x-px-csrf": "guess"}).status_code == 403
        assert hpost(c, tok, body, **{"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.post("/api/chat", content="a=1", headers={"origin": "http://testserver", "x-px-csrf": tok,
                                                           "content-type": "application/x-www-form-urlencoded"}).status_code == 415
        assert c.get("/api/chat").status_code == 405
        assert hpost(c, tok, body).status_code == 200


def test_relays_the_prompt_with_the_token_and_a_fixed_session_and_never_leaks_the_token():
    seen = {}

    def handler(req):
        seen["auth"], seen["url"] = req.headers["authorization"], str(req.url)
        seen["body"] = req.read().decode()
        return httpx.Response(200, json={"result": "Flat. No working orders."})

    app = app_with(agent(handler))
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        r = hpost(c, tok, {"prompt": "  what is open?  ", "session": "../../etc/passwd"})
    assert r.json() == {"reply": "Flat. No working orders."}
    assert seen["auth"] == "Bearer " + TOKEN and seen["url"] == "http://127.0.0.1:9315/ask"
    assert '"session":"desk"' in seen["body"].replace(" ", "") and "passwd" not in seen["body"]
    assert TOKEN not in r.text


@pytest.mark.parametrize("prompt", [None, "", "   ", 5, ["x"], "x" * (chat.MAX_PROMPT + 1)])
def test_bad_prompts_are_refused_before_the_agent_is_called(prompt):
    called = []
    app = app_with(agent(lambda r: called.append(r) or httpx.Response(200, json={"result": "x"})))
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, tok, {"prompt": prompt}).status_code == 400
    assert not called


def test_agent_failures_become_fixed_codes_not_agent_text():
    cases = [
        (lambda r: httpx.Response(401, text="Bearer secret-ish detail"), 502, "agent refused"),
        (lambda r: httpx.Response(200, text="not json"), 502, "bad agent reply"),
        (lambda r: httpx.Response(200, json={"result": 7}), 502, "bad agent reply"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom 10.0.0.9")), 502, "agent unreachable"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), 504, "agent timed out"),
    ]
    for handler, status, detail in cases:
        app = app_with(agent(handler))
        with TestClient(app) as c:
            tok = c.get("/api/csrf").json()["token"]
            r = hpost(c, tok, {"prompt": "hi"})
        assert (r.status_code, r.json()["detail"]) == (status, detail)


def test_off_is_503_and_state_says_so():
    app = app_with(None, cfg=None)
    with TestClient(app) as c:
        assert c.get("/api/chat/state").json() == {"enabled": False}
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, tok, {"prompt": "hi"}).status_code == 503


def test_state_says_enabled_when_configured():
    app = app_with(agent(lambda r: httpx.Response(200, json={"result": "x"})))
    with TestClient(app) as c:
        assert c.get("/api/chat/state").json() == {"enabled": True}


@pytest.mark.asyncio
async def test_a_second_question_while_one_is_running_is_busy():
    import asyncio
    gate = asyncio.Event()

    async def handler(req):
        await gate.wait()
        return httpx.Response(200, json={"result": "done"})

    ch = chat.Chat(("http://127.0.0.1:9315", TOKEN), httpx.MockTransport(handler))
    first = asyncio.create_task(ch.ask("one"))
    await asyncio.sleep(0.05)
    with pytest.raises(chat.ChatError) as e:
        await ch.ask("two")
    assert (e.value.status, e.value.code) == (429, "busy")
    gate.set()
    assert await first == "done"


def test_reply_is_capped():
    app = app_with(agent(lambda r: httpx.Response(200, json={"result": "y" * 50000})))
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert len(hpost(c, tok, {"prompt": "hi"}).json()["reply"]) == chat.MAX_REPLY


def test_record_only_has_no_chat_route(monkeypatch):
    monkeypatch.setenv("PX_RECORD_ONLY", "1")
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"), allowed_hosts={"testserver"}, connect=False)
    with TestClient(app) as c:
        assert c.get("/api/chat/state").status_code == 404
        assert c.post("/api/chat", json={"prompt": "hi"}).status_code in (404, 405)


# ---- the box itself -----------------------------------------------------------------------------------------------------

def test_chat_js_talks_only_to_chat_and_cannot_reach_the_order_side():
    js = (STATIC / "chat.js").read_text()
    posts = re.findall(r"""fetch\(\s*['"]([^'"]+)['"]\s*,\s*\{[^}]*method:\s*'POST'""", js)
    assert posts == ["/api/chat"]
    assert set(re.findall(r"""fetch\(\s*['"]([^'"]+)['"]""", js)) == {"/api/csrf", "/api/chat", "/api/chat/state"}
    assert not re.search(r"/api/(place|preview|flatten|stop|halt|reconcile|intent|readiness)", js, re.I)
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js     # a reply is text, never markup


def test_dock_is_hidden_while_streaming_and_wired_into_the_page():
    css, html = (STATIC / "desk.css").read_text(), (STATIC / "index.html").read_text()
    assert re.search(r"body\.streaming \.chat-fab,\s*body\.streaming \.chat-dock\{display:none\}", css)
    assert 'id="chat-fab"' in html and "hidden" in html.split('id="chat-fab"')[1].split(">")[0]
    assert '<script src="/static/chat.js"></script>' in html
