import ast
import pathlib

import httpx
import pytest

from projectx_desk import chat, llm_config, reply_guard as rg

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_module_is_pure():
    tree = ast.parse((ROOT / "projectx_desk" / "reply_guard.py").read_text())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            names.add((n.module or "") + "." + ",".join(a.name for a in n.names))
        elif isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
    assert names == {"re"}
    assert not any(isinstance(n, ast.Attribute) and n.attr == "write" for n in ast.walk(tree))


def test_plain_digest_answer_clean():
    assert rg.check("MES last 5400.25, up 3 ticks. BIAS: B lean long. FLOW: net buy 1200. Stop distance NA.") == []


def test_mentioning_think_in_prose_is_clean():
    assert rg.check("Some models print a <think> tag before answering; the desk strips none of it.") == []
    assert rg.check("The tag <think> opens and </think> closes.") == []


def test_reasoning_leak_flagged():
    assert "reasoning_leak" in rg.check("<think>The user wants the price so I should look it up in the digest first</think>MES 5400.")
    assert "reasoning_leak" in rg.check("<think>\nthinking, no close")


def test_tool_markup_flagged_and_only_markup_hard():
    t = '<tool_call>{"name": "place_order", "arguments": {}}</tool_call>'
    assert "tool_markup" in rg.check(t) and rg.only_markup(t)
    assert rg.only_markup('<function_calls><invoke name="x"><parameter name="a">1</parameter></invoke></function_calls>')
    mixed = 'MES is at 5400. <tool_call>{"name": "x"}</tool_call>'
    assert "tool_markup" in rg.check(mixed) and not rg.only_markup(mixed)
    assert not rg.only_markup("plain prose") and not rg.only_markup("")


def test_cjk_run_flagged_single_char_not():
    assert "cjk" in rg.check("MES is flat 价格很高 today")
    assert rg.check("The unit is 円 here") == []
    assert "cjk" not in rg.check("one 价 and another 格 apart")


def test_repeated_paragraph_flagged():
    p = "MES is trading above the 21 EMA on the five minute chart."
    assert "repeat" in rg.check(p + "\n\n" + p)
    assert rg.check(p + "\n\nNQ is flat on the day so far, nothing to add.") == []


def _http_chat(monkeypatch, text):
    cfg = llm_config.Resolved(llm_config.lp.preset("openai"), "https://api.openai.com/v1", "m", "sk-test1234567890abc")
    monkeypatch.setattr(llm_config, "resolve", lambda: cfg)
    monkeypatch.setattr(llm_config, "route", lambda: "http")
    tr = httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": text}}]}))
    return chat.Chat(None, transport=tr)


async def test_chat_http_flags_soft_and_rejects_only_markup(monkeypatch):
    r = await _http_chat(monkeypatch, "MES at 5400 and 价格很高").ask_full("hi")
    assert "cjk" in r["guard"] and r["reply"]
    with pytest.raises(chat.ChatError) as e:
        await _http_chat(monkeypatch, '<tool_call>{"name":"x"}</tool_call>').ask_full("hi")
    assert e.value.code == "bad_reply"
