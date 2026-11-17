"""AG-UI text subset: event order and field names, chunking, redaction, fixed error codes, input limits, the gate, the pin."""
import ast
import json
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

from projectx_desk import agui, chat
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub
from test_px_app import FakeClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "a" * 64


def app_with(handler, record_only=False):
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                     user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False,
                     record_only=record_only)
    app.state.chat = chat.Chat(("http://127.0.0.1:9315", TOKEN), httpx.MockTransport(handler))
    return app


def ok(text="hello"):
    return lambda r: httpx.Response(200, json={"result": text})


def hpost(c, tok, body, **hdr):
    h = {"origin": "http://testserver", "x-px-csrf": tok, "content-type": "application/json"}
    h.update(hdr)
    return c.post("/api/ag-ui", json=body, headers={k: v for k, v in h.items() if v is not None})


def events(resp):
    out = []
    for frame in resp.text.split("\n\n"):
        if frame:
            assert frame.startswith("data: ")
            out.append(json.loads(frame[6:]))
    return out


RUN = {"threadId": "t1", "runId": "r1", "messages": [{"id": "m1", "role": "user", "content": "what is MES doing"}]}


def test_event_order_and_field_names():
    ev = agui.run_events("t1", "r1", {"reply": "x" * 200, "lint": [], "ungrounded": []}, message_id="m")
    types = [e["type"] for e in ev]
    assert types == ["RUN_STARTED", "TEXT_MESSAGE_START"] + ["TEXT_MESSAGE_CONTENT"] * 3 + ["TEXT_MESSAGE_END", "RUN_FINISHED"]
    assert ev[0] == {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1"}
    assert ev[1] == {"type": "TEXT_MESSAGE_START", "messageId": "m", "role": "assistant"}
    assert set(ev[2]) == {"type", "messageId", "delta"}
    assert ev[-2] == {"type": "TEXT_MESSAGE_END", "messageId": "m"}
    assert ev[-1] == {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r1"}


def test_chunking_reassembles_to_the_reply():
    reply = "".join(chr(97 + i % 26) for i in range(1000))
    ev = agui.run_events("t", "r", {"reply": reply})
    got = "".join(e["delta"] for e in ev if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert got == reply and all(len(e["delta"]) <= agui.CHUNK for e in ev if e["type"] == "TEXT_MESSAGE_CONTENT")


def test_flags_ride_in_a_custom_event_before_run_finished():
    ev = agui.run_events("t", "r", {"reply": "go long", "lint": ["buy"], "ungrounded": ["7704"]})
    c = [e for e in ev if e["type"] == "CUSTOM"]
    assert c == [{"type": "CUSTOM", "name": "px.reply_flags", "value": {"lint": ["buy"], "ungrounded": ["7704"]}}]
    assert ev.index(c[0]) == len(ev) - 2


def test_route_streams_sse_and_secrets_are_redacted():
    secret = "sk-" + "A1b2C3d4" * 6
    app = app_with(ok("the key is " + secret + " ok"))
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        r = hpost(c, tok, RUN)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        ev = events(r)
        assert ev[0]["type"] == "RUN_STARTED" and ev[-1]["type"] == "RUN_FINISHED"
        assert secret not in r.text


def test_chat_error_becomes_run_error_with_a_fixed_code_never_provider_text():
    app = app_with(lambda r: httpx.Response(500, text="SECRET PROVIDER TEXT"))
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        r = hpost(c, tok, RUN)
        ev = events(r)
        assert [e["type"] for e in ev] == ["RUN_STARTED", "RUN_ERROR"]
        assert ev[1]["code"] == "agent_refused" and "SECRET" not in r.text
    assert agui.error_code("whatever the provider said") == "chat_error"


def test_busy_is_http_429():
    app = app_with(ok())
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        app.state.chat.lock._locked = True        # an in-flight question
        assert hpost(c, tok, RUN).status_code == 429


@pytest.mark.parametrize("bad", [
    {"messages": []}, {"messages": "hi"}, {"messages": [{"role": "system", "content": "x"}]},
    {"messages": [{"role": "user", "content": 5}]}, {"messages": [{"role": "assistant", "content": "x"}]},
    {"messages": [{"role": "user", "content": "x" * 2001}]}, {"threadId": 7, "messages": [{"role": "user", "content": "x"}]},
    {"messages": [{"role": "user", "content": "x"}] * 51}, [],
])
def test_malformed_or_oversized_input_is_refused(bad):
    with pytest.raises(agui.AguiError):
        agui.parse_run_input(bad)


def test_route_refuses_oversized_body_and_bad_input():
    app = app_with(ok())
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, tok, {"messages": []}).status_code == 400
        assert hpost(c, tok, {**RUN, "forwardedProps": "x" * 70_000}).status_code == 413


def test_tools_state_and_context_are_ignored_and_never_forwarded():
    seen = []

    def handler(r):
        seen.append(r.content.decode())
        return httpx.Response(200, json={"result": "fine"})
    app = app_with(handler)
    body = {**RUN, "tools": [{"name": "place_order"}], "state": {"k": "STATEVAL"}, "context": [{"description": "d", "value": "CTXVAL"}],
            "forwardedProps": {"a": "FWDVAL"}}
    assert agui.parse_run_input(body) == {"threadId": "t1", "runId": "r1", "prompt": "what is MES doing"}
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, tok, body).status_code == 200
    assert seen and not any(w in seen[0] for w in ("place_order", "STATEVAL", "CTXVAL", "FWDVAL"))


def test_last_user_message_is_the_prompt():
    b = {"messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}, {"role": "user", "content": "three"}]}
    assert agui.parse_run_input(b)["prompt"] == "three"


def test_gate_403s():
    app = app_with(ok())
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, tok, RUN, origin=None).status_code == 403
        assert hpost(c, tok, RUN, origin="http://evil.com").status_code == 403
        assert hpost(c, tok, RUN, **{"x-px-csrf": None}).status_code == 403
        assert hpost(c, tok, RUN, **{"x-px-csrf": "guess"}).status_code == 403
        assert hpost(c, tok, RUN, **{"sec-fetch-site": "cross-site"}).status_code == 403


def test_record_only_has_no_route():
    app = app_with(ok(), record_only=True)
    assert "/api/ag-ui" not in {r.path for r in app.routes}


def test_module_is_pure():
    tree = ast.parse((ROOT / "projectx_desk" / "agui.py").read_text())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            names.add((n.module or "") + "." + ",".join(a.name for a in n.names))
        elif isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
    for bad in ("guard", "client", "hub", "mcp", "app", "chat", "httpx"):
        assert not any(bad in x.replace("json", "") for x in names), (bad, names)
    assert not any(isinstance(n, ast.Attribute) and n.attr == "write" for n in ast.walk(tree))
