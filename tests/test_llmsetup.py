"""Ask box provider picker (static/llmsetup.js): node-run pure rules plus static pins (no innerHTML or eval, fixed URL set, no key leaks)."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
SRC = (STATIC / "llmsetup.js").read_text(encoding="utf-8")
ALLOWED = {"/api/csrf", "/api/llm/detect", "/api/llm/config", "/api/llm/clear", "/api/llm/test", "/api/llm/login/start", "/api/llm/login/status"}

OR = {"id": "openrouter", "label": "OpenRouter", "kind": "http", "needs_key": True, "takes_key": True, "url_editable": False,
      "base_url": "https://openrouter.ai/api/v1", "default_model": "deepseek/deepseek-v4-flash"}
OAI = dict(OR, id="openai", label="OpenAI")
OLL = {"id": "ollama", "label": "Ollama", "kind": "http", "needs_key": False, "takes_key": False, "url_editable": True,
       "base_url": "http://127.0.0.1:11434/v1", "default_model": "llama3.2"}
CUS = {"id": "custom", "label": "Custom endpoint", "kind": "http", "needs_key": False, "takes_key": True, "url_editable": True,
       "base_url": "", "default_model": ""}
HER = dict(CUS, id="hermes", label="Hermes")
CLI = {"id": "claude_cli", "label": "Claude", "kind": "cli", "needs_key": False, "takes_key": False, "url_editable": False,
       "base_url": "", "default_model": ""}


def run(expr, data=None):
    js = ("const L=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));"
          "console.log(JSON.stringify((" + expr + ")(L,d)))")
    out = subprocess.run(["node", "-e", js, str(STATIC / "llmsetup.js")], input=json.dumps(data), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_url_rules_match_the_python_guard():
    urls = ["https://openrouter.ai/api/v1/", "http://127.0.0.1:1234/v1", "http://localhost:11434/v1", "http://[::1]:8000/v1",
            "http://evil.com/v1", "http://192.168.1.5/v1", "https://u:p@x.com/v1", "https://x.com/v1?a=1", "https://x.com/v1#f",
            "ftp://x.com", "", "   ", "https://" + "a" * 220 + ".com", "http://127.0.0.1.evil.com/v1", "http://127.0.0.1@evil.com/v1"]
    got = run("(L,d)=>d.map(u=>L.validateBaseUrl(u))", urls)
    assert got == ["https://openrouter.ai/api/v1", "http://127.0.0.1:1234/v1", "http://localhost:11434/v1", "http://[::1]:8000/v1",
                   None, None, None, None, None, None, None, None, None, None, None]
    lan = run("(L,d)=>d.map(([u,l])=>L.validateRemoteAgentUrl(u,l))",
              [["http://192.168.2.5:9/v1", True], ["http://192.168.2.5:9/v1", False], ["http://100.100.1.1/v1", True],
               ["http://172.32.0.1/v1", True], ["http://myhost.lan/v1", True], ["https://anywhere.com/x", False], ["http://127.0.0.1/v1", False]])
    assert lan == ["http://192.168.2.5:9/v1", None, "http://100.100.1.1/v1", None, None, "https://anywhere.com/x", "http://127.0.0.1/v1"]


@needs_node
def test_key_and_model_rules():
    k = run("(L,d)=>d.map(([c,k])=>L.keyShapeError(c,k))", [
        [OR, "sk-or-abc"], [OR, "sk-abc"], [OR, ""], [OR, "sk-or- x"], [OR, "sk-or-" + "a" * 300],
        [OAI, "sk-or-abc"], [OAI, "sk-ant-abc"], [OAI, "sk-proj-abc"], [{"id": "anthropic", "label": "Anthropic"}, "sk-ant-x1"],
        [{"id": "anthropic", "label": "Anthropic"}, "sk-x1"]])
    assert [x is None for x in k] == [True, False, False, False, False, False, False, True, True, False]
    assert k[2] == "Paste your OpenRouter key first."
    m = run("(L,d)=>d.map(([c,m])=>L.modelError(c,m))", [[OR, ""], [CUS, ""], [CUS, "my model"], [CUS, "a/b:c@1.2"], [OR, "-bad"]])
    assert [x is None for x in m] == [True, False, False, True, False]


@needs_node
def test_form_validation_and_request_body():
    v = run("(L,d)=>d.map(([c,f])=>L.validateForm(c,f).errors)", [
        [OR, {"key": "", "model": "", "key_present": True}],                       # saved key stays, blank model uses the default
        [OR, {"key": "", "model": "", "key_present": False}],                      # no key anywhere
        [OR, {"key": "sk-ant-zzz", "model": "x", "key_present": True}],
        [OLL, {"base_url": "", "model": ""}],                                      # blank local URL falls back to the preset
        [OLL, {"base_url": "http://192.168.2.5:11434/v1", "model": ""}],
        [CUS, {"base_url": "", "model": "m"}],
        [CUS, {"base_url": "https://api.example.com/v1", "model": "m"}],
        [HER, {"base_url": "http://192.168.2.5:9/v1", "model": "m", "lan_http": False}],
        [HER, {"base_url": "http://192.168.2.5:9/v1", "model": "m", "lan_http": True}],
        [CLI, {}]])
    assert v[0] == {} and "key" in v[1] and "key" in v[2] and v[3] == {} and "url" in v[4] and "url" in v[5] and v[6] == {}
    assert "url" in v[7] and v[8] == {} and v[9] == {}
    b = run("(L,d)=>d.map(([c,f])=>L.buildConfigBody(c,f))", [
        [OR, {"model": " m ", "key": " sk-or-abc ", "base_url": "https://ignored"}], [OR, {"model": "", "key": ""}],
        [OLL, {"model": "llama3.2", "base_url": "http://127.0.0.1:11434/v1", "key": "ignored"}],
        [HER, {"model": "m", "base_url": "https://h/v1", "lan_http": True, "key": "k"}], [CLI, {"model": "x", "key": "y"}]])
    assert b[0] == {"provider": "openrouter", "model": "m", "key": "sk-or-abc"}
    assert b[1] == {"provider": "openrouter", "model": ""}                          # blank key is omitted so the saved one stays
    assert b[2] == {"provider": "ollama", "model": "llama3.2", "base_url": "http://127.0.0.1:11434/v1"}
    assert b[3] == {"provider": "hermes", "model": "m", "base_url": "https://h/v1", "lan_http": True, "key": "k"}
    assert b[4] == {"provider": "claude_cli"}


@needs_node
def test_error_words_login_link_and_route_text():
    codes = ["unauthorized", "rate_limited", "model_blocked", "unreachable", "timeout", "bad_reply", "not_configured", "weird<script>"]
    w = run("(L,d)=>d.map(c=>L.testWords(c))", codes)
    assert all(w) and "workspace blocks this model" in w[2] and "<" not in w[7]
    assert all(run("(L,d)=>d.map(c=>L.saveWords(c))", ["bad_url", "bad_key", "x y"]))
    ok = run("(L,d)=>d.map(u=>L.isOpenRouterUrl(u))", ["https://openrouter.ai/auth?x=1", "http://openrouter.ai/auth", "https://openrouter.ai.evil.com/auth",
                                                        "https://evil.com/https://openrouter.ai/", "javascript:alert(1)", None, "https://openrouter.ai@evil.com/"])
    assert ok == [True, False, False, False, False, False, False]
    view = {"configured": True, "provider": "openrouter", "kind": "http", "model": "m"}
    r = run("(L,d)=>[L.routeLabel(d.v,d.c),L.digestNotice(d.v,d.c),L.routeLabel({configured:false},[]),L.digestNotice(null,[])]", {"v": view, "c": [OR]})
    assert r[0] == "OpenRouter (m)" and "OpenRouter (m)" in r[1] and "no account ids" in r[1] and "this computer" in r[2] and "stay" in r[3]


# ---- static pins ----------------------------------------------------------------------------------------------------------------

def test_no_dynamic_html_or_eval():
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout('", 'setTimeout("'):
        assert bad not in SRC, bad


def test_only_the_allowed_urls_are_fetched():
    paths = set(re.findall(r"""['"](/api/[A-Za-z0-9_/]+)""", SRC))
    assert paths and paths <= ALLOWED, paths - ALLOWED
    assert "/api/chat" not in SRC.replace("/api/chat/", "")
    for call in re.findall(r"fetch\(([^,)]*)", SRC):
        assert "http" not in call, call
    assert not re.search(r"\bXMLHttpRequest\b|\bWebSocket\b|sendBeacon|EventSource", SRC)


def test_a_key_never_reaches_the_dom_storage_or_console():
    assert not re.search(r"\bconsole\b|localStorage|sessionStorage|indexedDB|document\.cookie|history\.(push|replace)State|location\.(hash|search)", SRC)
    assert "keyIn.type = 'password'" in SRC and "keyIn.value = ''" in SRC
    # the key input is only ever read inside read(), then wiped after the POST; nothing assigns its value to text, attributes or titles
    uses = [m.start() for m in re.finditer(r"keyIn\.value", SRC)]
    assert len(uses) == 3                                   # initial blank, the read, the wipe after sending
    assert not re.search(r"(textContent|setAttribute\([^)]*|title|placeholder|\.href|\.src)\s*=?[^;\n]*\bkeyIn\b", SRC.replace("keyIn.placeholder = v && v.key_present ? 'Saved. Type to replace.' : 'Paste your key'", ""))
    # error text built from a typed value would leak it: server codes go through the fixed word tables only
    assert "safeCode" in SRC


def test_wiring_in_page_and_chat_box():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.count('src="/static/llmsetup.js"') == 1 and html.count('href="/static/llmsetup.css"') == 1
    assert html.index("/static/chat.js") < html.index("/static/llmsetup.js")
    assert "pxLlmSetup.open()" in (STATIC / "chat.js").read_text(encoding="utf-8")
    css = (STATIC / "llmsetup.css").read_text(encoding="utf-8")
    assert "body.streaming .llm-panel" in css and "body.streaming .llm-route" in css
    for f in ("llmsetup.js", "llmsetup.css"):
        t = (STATIC / f).read_text(encoding="utf-8")
        assert "—" not in t and "–" not in t, f
