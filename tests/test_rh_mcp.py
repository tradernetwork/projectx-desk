"""Phase 0, task 1: the mock Robinhood MCP server and the read-only MCP client."""
import httpx
import pytest

from projectx_desk import dev_robinhood
from projectx_desk.rh import mcp
from projectx_desk.rh.mcp import McpError, READ_TOOLS, Client


class Provider:
    """Token provider backed by the mock's own issuer."""

    def __init__(self, state, first=None):
        self.state, self.first, self.forced = state, first, 0

    async def access_token(self):
        return self.first or self.state.issue("c")["access_token"]

    async def force_refresh(self):
        self.forced += 1
        self.first = None                                             # a real store hands out the fresh token next
        return self.state.issue("c")["access_token"]


def make(accounts=None, first=None, **kw):
    app = dev_robinhood.make_app(accounts)
    st = app.state.mock
    prov = Provider(st, first)
    c = Client("http://127.0.0.1/mcp", prov, transport=httpx.ASGITransport(app=app), **kw)
    return app, st, prov, c


async def test_round_trip_initialize_list_call():
    _, st, _, c = make()
    tools = await c.list_tools()
    names = {t["name"] for t in tools}
    assert READ_TOOLS <= names
    assert {"place_equity_order", "cancel_equity_order"} <= names      # listed, never callable
    data = await c.call("get_accounts")
    assert [a["agentic_allowed"] for a in data["accounts"]] == [True, False]
    assert st.calls == ["get_accounts"]


async def test_call_with_args_and_envelope_unwrapped():
    _, _, _, c = make()
    data = await c.call("get_equity_quotes", {"symbols": ["AAA", "BBB"]})
    assert [r["quote"]["symbol"] for r in data["results"]] == ["AAA", "BBB"]


@pytest.mark.parametrize("tool", ["place_equity_order", "cancel_equity_order", "exercise_option", "nonsense"])
async def test_write_tools_blocked_before_any_request(tool):
    app, st, _, c = make()
    with pytest.raises(McpError) as e:
        await c.call(tool, {"symbol": "AAA"})
    assert e.value.code == "write_blocked"
    assert st.requests == 0                                            # not even initialize was sent


async def test_limiter_four_per_second_with_injected_clock():
    now = [0.0]
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        now[0] += s

    _, _, _, c = make(clock=lambda: now[0], sleep=fake_sleep)
    lim = c._limiter
    for _ in range(4):
        await lim.acquire()
    assert slept == []
    await lim.acquire()                                               # fifth inside the same second must wait
    assert slept and now[0] >= 1.0
    # many more never exceed 4 in any 1 s window
    stamps = []
    for _ in range(12):
        await lim.acquire()
        stamps.append(now[0])
    for i in range(len(stamps) - 4):
        assert stamps[i + 4] - stamps[i] >= 1.0 - 1e-9


async def test_401_refreshes_once_then_succeeds():
    _, st, prov, c = make(first="expired-token")
    data = await c.call("get_accounts")
    assert prov.forced == 1 and "accounts" in data


async def test_401_twice_is_unauthorized_no_loop():
    app = dev_robinhood.make_app()

    class Bad:
        forced = 0

        async def access_token(self):
            return "nope"

        async def force_refresh(self):
            Bad.forced += 1
            return "still-nope"

    c = Client("http://127.0.0.1/mcp", Bad(), transport=httpx.ASGITransport(app=app))
    with pytest.raises(McpError) as e:
        await c.call("get_accounts")
    assert e.value.code == "unauthorized" and Bad.forced == 1


async def test_no_token_provider_is_unauthorized():
    app = dev_robinhood.make_app()
    c = Client("http://127.0.0.1/mcp", None, transport=httpx.ASGITransport(app=app))
    with pytest.raises(McpError) as e:
        await c.call("get_accounts")
    assert e.value.code == "unauthorized"


async def _fault(fault):
    app, st, _, c = make()
    st.fault = fault
    with pytest.raises(McpError) as e:
        await c.call("get_accounts")
    return e.value, st


async def test_redirect_refused_and_not_followed():
    err, st = await _fault("redirect")
    assert err.code == "redirect" and st.requests == 1                 # never followed to evil.invalid


async def test_rate_limited_429():
    err, _ = await _fault("rate")
    assert err.code == "rate_limited"


async def test_5xx_retried_once_then_unreachable_and_provider_text_never_leaks():
    err, st = await _fault("500")
    assert err.code == "unreachable" and st.requests == 2
    assert "SECRET-TEXT" not in str(err) and "SECRET-TEXT" not in repr(err)


async def test_oversized_body_too_big():
    err, _ = await _fault("huge")
    assert err.code == "too_big"


async def test_invalid_json_bad_reply():
    err, _ = await _fault("badjson")
    assert err.code == "bad_reply"


async def test_transport_failure_retries_once_then_unreachable():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom with secret detail")

    class P:
        async def access_token(self):
            return "t"

    c = Client("http://127.0.0.1/mcp", P(), transport=httpx.MockTransport(handler))
    with pytest.raises(McpError) as e:
        await c.call("get_accounts")
    assert e.value.code == "unreachable" and len(calls) == 2 and "secret" not in str(e.value)


async def test_timeout_code():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    class P:
        async def access_token(self):
            return "t"

    c = Client("http://127.0.0.1/mcp", P(), transport=httpx.MockTransport(handler))
    with pytest.raises(McpError) as e:
        await c.call("get_accounts")
    assert e.value.code == "timeout"


async def test_invalid_jsonrpc_error_is_bad_reply_without_text():
    def handler(request):
        import json
        body = json.loads(request.content)
        if body.get("id") is None:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "error": {"code": -1, "message": "PROVIDER-FREE-TEXT"}})

    class P:
        async def access_token(self):
            return "t"

    c = Client("http://127.0.0.1/mcp", P(), transport=httpx.MockTransport(handler))
    with pytest.raises(McpError) as e:
        await c.call("get_accounts")
    assert e.value.code == "bad_reply" and "PROVIDER" not in str(e.value)


async def test_sse_reply_is_parsed():
    import json

    def handler(request):
        body = json.loads(request.content)
        if body.get("id") is None:
            return httpx.Response(202)
        if body["method"] == "initialize":
            res = {}
        else:
            res = {"structuredContent": {"data": {"ok": 1}}}
        sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": res}) + "\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    class P:
        async def access_token(self):
            return "t"

    c = Client("http://127.0.0.1/mcp", P(), transport=httpx.MockTransport(handler))
    assert await c.call("get_accounts") == {"ok": 1}


def test_plain_http_to_a_remote_host_refused():
    with pytest.raises(McpError):
        Client("http://agent.robinhood.com/mcp/trading", None)
    Client("https://agent.robinhood.com/mcp/trading", None)
    Client("http://127.0.0.1:9/mcp", None)


async def test_mock_write_without_an_agentic_account_is_refused_even_if_called_raw():
    app, st, _, c = make()
    await c.list_tools()
    tok = st.issue("c")["access_token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as h:
        r = await h.post("/mcp", headers={"Authorization": f"Bearer {tok}"}, json={
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "place_equity_order", "arguments": {}}})
    res = r.json()["result"]
    assert res["isError"] is True and "account_not_allowed" in res["content"][0]["text"]
    assert not st.place_calls


async def test_historicals_is_a_read_tool_and_round_trips():
    assert "get_equity_historicals" in READ_TOOLS
    _, st, _, c = make()
    data = await c.call("get_equity_historicals", {"symbols": ["QQQ"], "interval": "5minute",
                                                   "start_time": "2026-11-19T14:30:00Z", "end_time": "2026-11-19T15:00:00Z"})
    bars = data["results"][0]["bars"]
    assert data["results"][0]["symbol"] == "QQQ" and len(bars) == 6
    assert {"begins_at", "open_price", "close_price", "high_price", "low_price", "volume"} <= set(bars[0])
    assert st.calls == ["get_equity_historicals"]
