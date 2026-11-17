"""The Ask box through a configured HTTP provider: routes, key never returned, chat routes to llm_chat, images refused."""
import contextlib
import json

import httpx
from fastapi.testclient import TestClient

from projectx_desk import chat
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub
from test_px_app import FakeClient

KEY = "sk-or-v1-" + "a" * 40
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


@contextlib.contextmanager
def make(monkeypatch, tmp_path, handler):
    monkeypatch.setenv("PX_LLM_CONFIG", str(tmp_path / "llm.json"))
    monkeypatch.delenv("PX_LLM_KEY", raising=False)
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                     user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False)
    app.state.chat = chat.Chat(None, httpx.MockTransport(handler))
    with TestClient(app) as c:
        yield c, c.get("/api/csrf").json()["token"]


def post(c, tok, path, body):
    return c.post(path, json=body, headers={"origin": "http://testserver", "x-px-csrf": tok, "content-type": "application/json"})


def test_config_roundtrip_never_returns_the_key_and_chat_routes_to_the_provider(monkeypatch, tmp_path):
    seen = {}

    def handler(req):
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "MES is at 7704.5 per the digest."}}],
                                         "usage": {"prompt_tokens": 9, "completion_tokens": 7}})

    with make(monkeypatch, tmp_path, handler) as (c, tok):
        assert c.get("/api/chat/state").json() == {"enabled": False}
        r = post(c, tok, "/api/llm/config", {"provider": "openrouter", "key": KEY})
        assert r.status_code == 200 and KEY not in r.text and r.json()["key_present"] is True
        assert KEY not in c.get("/api/llm/config").text
        assert c.get("/api/chat/state").json() == {"enabled": True}
        r = post(c, tok, "/api/chat", {"prompt": "where is MES?"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert "7704.5" in j["reply"] and "lint" in j and "ungrounded" in j and j["usage"]["output_tokens"] == 7
        assert seen["url"].startswith("https://openrouter.ai/api/v1") and seen["auth"] == "Bearer " + KEY
        assert KEY not in r.text


def test_images_refused_on_the_http_path_and_a_bad_key_shape_is_a_400(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, lambda req: httpx.Response(200, json={})) as (c, tok):
        assert post(c, tok, "/api/llm/config", {"provider": "openrouter", "key": "sk-ant-nope"}).status_code == 400
        assert post(c, tok, "/api/llm/config", {"provider": "ollama"}).status_code == 200
        assert post(c, tok, "/api/chat", {"prompt": "look", "images": [{"data": PNG}]}).status_code == 400


def test_llm_posts_need_the_gate(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, lambda req: httpx.Response(200, json={})) as (c, tok):
        for path in ("/api/llm/config", "/api/llm/clear", "/api/llm/test", "/api/llm/login/start"):
            assert c.post(path, json={}).status_code == 403
            assert c.post(path, json={}, headers={"origin": "http://evil.example", "x-px-csrf": tok}).status_code == 403
