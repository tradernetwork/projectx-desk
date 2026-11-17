import asyncio
import json

import httpx
import pytest

from projectx_desk import llm_chat as ch
from projectx_desk import llm_config as lc
from projectx_desk import llm_providers as lp

KEY = "sk-or-SECRETKEY1234567890"
TURNS = [{"role": "user", "content": "what is MES doing"}]


def cfg(provider="openrouter", key=KEY, url=None, model=None):
    p = lp.preset(provider)
    return lc.Resolved(p, url or p.base_url, p.default_model or "m" if model is None else model, key)


def oai(text="hello", usage=True):
    d = {"choices": [{"message": {"content": text}}]}
    if usage:
        d["usage"] = {"prompt_tokens": 11, "completion_tokens": 3}
    return httpx.Response(200, json=d)


def transport(handler):
    return httpx.MockTransport(handler)


async def run(c, handler, turns=TURNS, system="sys"):
    return await ch.ask(c, system, turns, transport=transport(handler))


async def code_of(c, handler, turns=TURNS):
    with pytest.raises(ch.LlmError) as e:
        await run(c, handler, turns)
    assert KEY not in repr(e.value) + str(e.value)
    return e.value.code


async def test_openrouter_shape_and_headers():
    seen = {}

    def h(req):
        seen["url"], seen["h"], seen["b"] = str(req.url), req.headers, json.loads(req.content)
        return oai()

    r = await run(cfg(), h, system="SYS")
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["h"]["authorization"] == "Bearer " + KEY and seen["h"]["x-title"]
    b = seen["b"]
    assert b["model"] == "deepseek/deepseek-v4-flash" and b["max_tokens"] == ch.MAX_TOKENS
    assert b["messages"][0] == {"role": "system", "content": "SYS"} and b["messages"][1]["role"] == "user"
    assert "tools" not in b and "stream" not in b
    assert (r.text, r.input_tokens, r.output_tokens) == ("hello", 11, 3)
    assert KEY not in repr(r.usage())


@pytest.mark.parametrize("prov,url", [("openai", "https://api.openai.com/v1/chat/completions"),
                                      ("venice", "https://api.venice.ai/api/v1/chat/completions")])
async def test_bearer_profiles(prov, url):
    seen = {}

    def h(req):
        seen["u"], seen["a"], seen["t"] = str(req.url), req.headers.get("authorization"), req.headers.get("x-title")
        return oai()

    await run(cfg(prov), h)
    assert seen == {"u": url, "a": "Bearer " + KEY, "t": None}


async def test_local_no_key_header():
    seen = {}

    def h(req):
        seen["a"], seen["u"] = req.headers.get("authorization"), str(req.url)
        return oai()

    await run(cfg("ollama", key=""), h)
    assert seen["a"] is None and seen["u"] == "http://127.0.0.1:11434/v1/chat/completions"


async def test_custom_and_hermes_optional_key():
    seen = []

    def h(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        return oai()

    await run(cfg("custom", key="", url="https://x.example/v1"), h)
    await run(cfg("hermes", key="k", url="https://h.example/v1"), h)
    assert seen == [("https://x.example/v1/chat/completions", None), ("https://h.example/v1/chat/completions", "Bearer k")]


async def test_anthropic_native():
    seen = {}

    def h(req):
        seen["u"], seen["h"], seen["b"] = str(req.url), req.headers, json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "a"}, {"type": "tool_use"}, {"type": "text", "text": "b"}],
                                         "usage": {"input_tokens": 5, "output_tokens": 2}})

    r = await run(cfg("anthropic", key="sk-ant-SECRET123"), h, system="SYS")
    assert seen["u"] == "https://api.anthropic.com/v1/messages"
    assert seen["h"]["x-api-key"] == "sk-ant-SECRET123" and seen["h"]["anthropic-version"] and "authorization" not in seen["h"]
    assert seen["b"]["system"] == "SYS" and seen["b"]["max_tokens"] == ch.MAX_TOKENS
    assert all(m["role"] != "system" for m in seen["b"]["messages"])
    assert (r.text, r.input_tokens, r.output_tokens) == ("ab", 5, 2)


async def test_usage_missing_is_none():
    r = await run(cfg(), lambda req: oai(usage=False))
    assert r.input_tokens is None and r.output_tokens is None


async def test_reply_cap_and_redaction():
    r = await run(cfg(), lambda req: oai("x" * 20000))
    assert len(r.text) == ch.MAX_REPLY
    r = await run(cfg(), lambda req: oai("your key is " + KEY + " ok"))
    assert KEY not in r.text


async def test_redirect_refused():
    calls = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    assert await code_of(cfg(), h) == "unreachable"
    assert calls == ["https://openrouter.ai/api/v1/chat/completions"]


async def test_size_cap():
    big = b'{"choices":[{"message":{"content":"' + b"a" * (ch.MAX_RESPONSE + 10) + b'"}}]}'
    assert await code_of(cfg(), lambda req: httpx.Response(200, content=big)) == "bad_reply"


@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (402, "unauthorized"), (403, "unauthorized"), (429, "rate_limited"),
                                         (400, "bad_request"), (404, "bad_request"), (422, "bad_request"), (500, "unreachable"),
                                         (503, "unreachable"), (418, "bad_reply")])
async def test_status_codes(status, code):
    body = {"error": {"message": "provider says " + KEY}}
    assert await code_of(cfg(), lambda req: httpx.Response(status, json=body)) == code


async def test_model_blocked_only_openrouter_404():
    body = {"error": {"message": "Model blocked by guardrail"}}
    assert await code_of(cfg(), lambda req: httpx.Response(404, json=body)) == "model_blocked"
    assert await code_of(cfg("openai"), lambda req: httpx.Response(404, json=body)) == "bad_request"


async def test_provider_text_never_leaks():
    with pytest.raises(ch.LlmError) as e:
        await run(cfg(), lambda req: httpx.Response(400, text="SECRET-PROVIDER-TEXT"))
    assert "SECRET-PROVIDER-TEXT" not in repr(e.value) + str(e.value)


async def test_network_errors():
    def boom(req):
        raise httpx.ConnectError("refused " + KEY)

    def slow(req):
        raise httpx.ReadTimeout("t")

    assert await code_of(cfg(), boom) == "unreachable"
    assert await code_of(cfg(), slow) == "timeout"


@pytest.mark.parametrize("resp", [httpx.Response(200, text="not json"), httpx.Response(200, json=[1]), httpx.Response(200, json={}),
                                  httpx.Response(200, json={"choices": []}), httpx.Response(200, json={"choices": [{"message": {"content": 5}}]}),
                                  httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})])
async def test_bad_reply(resp):
    assert await code_of(cfg(), lambda req: resp) == "bad_reply"
    assert await code_of(cfg("anthropic", key="sk-ant-x1"), lambda req: resp) == "bad_reply"


async def test_not_configured():
    assert await code_of(cfg(key=""), lambda req: oai()) == "not_configured"
    assert await code_of(cfg("custom", key="", url="https://x.example/v1", model=""), lambda req: oai()) == "not_configured"
    with pytest.raises(ch.LlmError) as e:
        await ch.ask(None, "s", TURNS)
    assert e.value.code == "not_configured"


@pytest.mark.parametrize("turns", [[], None, [{"role": "system", "content": "x"}], [{"role": "user", "content": ""}],
                                   [{"role": "assistant", "content": "x"}], [{"role": "user", "content": "x" * 5000}],
                                   [{"role": "user", "content": "a"}] * 25, [{"role": "user", "content": 3}]])
async def test_bad_turns(turns):
    assert await code_of(cfg(), lambda req: oai(), turns) == "bad_request"


async def test_concurrency_second_is_rate_limited_and_released():
    gate = asyncio.Event()

    async def slow(req):
        await gate.wait()
        return oai()

    first = asyncio.create_task(ch.ask(cfg(), "s", TURNS, transport=transport(slow)))
    await asyncio.sleep(0.05)
    with pytest.raises(ch.LlmError) as e:
        await ch.ask(cfg(), "s", TURNS, transport=transport(lambda req: oai()))
    assert e.value.code == "rate_limited"
    gate.set()
    assert (await first).text == "hello"
    assert (await run(cfg(), lambda req: oai())).text == "hello"       # released
    assert await code_of(cfg(), lambda req: httpx.Response(500)) == "unreachable"
    assert (await run(cfg(), lambda req: oai())).text == "hello"       # released after an error too


async def test_test_helper():
    seen = {}

    def h(req):
        seen["b"] = json.loads(req.content)
        return oai()

    r = await ch.test(cfg(), transport=transport(h))
    assert r["ok"] is True and seen["b"]["max_tokens"] == 8 and KEY not in repr(r)
    r = await ch.test(cfg(), transport=transport(lambda req: httpx.Response(401)))
    assert r == {"ok": False, "code": "unauthorized"}


def test_system_prompt():
    s = ch.build_system("MES 5000")
    assert "NA" in s and "never" in s.lower() and s.endswith("MES 5000")
    assert len(ch.build_system("x" * 100000)) < len(ch.RULES) + ch.MAX_DIGEST + 50
    assert ch.build_system(None).startswith(ch.RULES)
