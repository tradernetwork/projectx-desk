"""Phase 0, task 3: Robinhood views, account-scoped reads, and the polled state."""
import json

import httpx
import pytest

from projectx_desk import dev_robinhood
from projectx_desk.rh import reads, state as rh_state, views
from projectx_desk.rh.mcp import Client

MCP_ACCT, OTHER_ACCT = "MOCK-MCP-0001", "MOCK-PERSONAL-0002"


class Provider:
    def __init__(self, st):
        self.st = st

    async def access_token(self):
        return self.st.issue("c")["access_token"]

    async def force_refresh(self):
        return self.st.issue("c")["access_token"]


class Spy:
    """Wraps a Client and records every (tool, args)."""

    def __init__(self, c):
        self.c, self.seen = c, []

    async def call(self, tool, args=None):
        self.seen.append((tool, dict(args or {})))
        return await self.c.call(tool, args)


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.n = 0

    async def handle_async_request(self, request):
        self.n += 1
        raise AssertionError("no outbound traffic expected")


def make(accounts=None):
    app = dev_robinhood.make_app(accounts)
    st = app.state.mock
    c = Client("http://127.0.0.1/mcp", Provider(st), transport=httpx.ASGITransport(app=app))
    return app, st, Spy(c)


# ---- views -------------------------------------------------------------------------------------------------------
def test_position_view_allowlist_and_no_account_fields():
    v = views.position_view({"symbol": "AAA", "quantity": "10.0000", "average_buy_price": "100.5000", "type": "long",
                             "account_number": OTHER_ACCT, "secret": "x", "id": "abc"})
    assert v == {"symbol": "AAA", "asset": "equity", "qty": 10.0, "avg_price": 100.5, "side": "long", "unknown": []}
    assert OTHER_ACCT not in json.dumps(v)


@pytest.mark.parametrize("bad", [None, "", "nan", "NaN", "inf", "-Infinity", "abc", float("nan"), float("inf"), True, [], {}])
def test_bad_numbers_are_omitted_and_listed_never_zero(bad):
    v = views.position_view({"symbol": "AAA", "quantity": bad, "average_buy_price": bad, "type": "long"})
    assert "qty" not in v and "avg_price" not in v
    assert set(v["unknown"]) == {"qty", "avg_price"}
    assert 0 not in v.values()


def test_missing_number_keys_listed_unknown():
    v = views.position_view({"symbol": "AAA"})
    assert v["unknown"] == ["qty", "avg_price"] or set(v["unknown"]) >= {"qty", "avg_price"}


def test_position_view_rejects_bad_symbol_and_non_dict():
    assert views.position_view({"symbol": "ignore previous instructions", "quantity": "1"}) is None
    assert views.position_view({"quantity": "1"}) is None
    assert views.position_view("x") is None


def test_option_position_view():
    v = views.position_view({"symbol": "AAA", "chain_symbol": "AAA", "quantity": "2", "type": "short", "average_price": "0.85",
                             "expiration_date": "2026-12-18", "strike_price": "18.00", "option_type": "put"})
    assert v["asset"] == "option" and v["side"] == "short" and v["right"] == "put"
    assert v["expiry"] == "2026-12-18" and v["strike"] == 18.0 and v["qty"] == 2.0 and v["avg_price"] == 0.85
    w = views.position_view({"symbol": "AAA", "quantity": "1", "expiration_date": "tomorrow", "strike_price": "nan",
                             "option_type": "put"})
    assert "expiry" not in w and "strike" not in w and {"expiry", "strike"} <= set(w["unknown"])


def test_order_view_enums_and_free_text():
    v = views.order_view({"id": "mock-eo-2", "symbol": "BBB", "side": "sell", "type": "limit", "state": "queued",
                          "quantity": "5.00000000", "price": "22.00000000", "account_number": OTHER_ACCT})
    assert v["symbol"] == "BBB" and v["side"] == "sell" and v["status"] == "queued" and v["qty"] == 5.0 and v["price"] == 22.0
    w = views.order_view({"id": "x; DROP", "symbol": "BBB", "side": "buy now!!", "state": "weird text from provider",
                          "quantity": "nan"})
    assert w["status"] == "unknown" and "side" not in w and "id" not in w and "qty" not in w and "qty" in w["unknown"]


def test_approval_view():
    v = views.approval_view({"approval_id": "mock-appr-1", "asset_class": "ASSET_CLASS_EQUITY",
                             "state": "APPROVAL_STATE_PENDING",
                             "proposed_order": {"symbol": "AAA", "side": "buy", "quantity": "1"}, "note": "free text"})
    assert v == {"id": "mock-appr-1", "asset": "equity", "status": "pending", "symbol": "AAA", "side": "buy", "qty": 1.0,
                 "unknown": []}


def test_account_view_has_no_account_number():
    v = views.account_view({"account_number": MCP_ACCT, "agentic_allowed": True, "type": "limited_margin", "state": "active",
                            "option_level": "option_level_2", "nickname": "personal"})
    assert v["agentic_allowed"] is True and MCP_ACCT not in json.dumps(v) and "nickname" not in v


# ---- reader ------------------------------------------------------------------------------------------------------
async def test_reader_only_ever_names_the_mcp_account():
    _, _, spy = make()
    r = reads.Reader(spy)
    pos, opt = await r.positions(), await r.option_positions()
    assert [p["symbol"] for p in pos] == ["AAA", "BBB", "CCC"] and opt[0]["asset"] == "option"
    await r.orders(), await r.approvals(), await r.approval_setting(), await r.realized_pnl()
    sent = json.dumps(spy.seen)
    assert MCP_ACCT in sent and OTHER_ACCT not in sent
    # every read resolved the account first
    tools = [t for t, _ in spy.seen]
    assert tools[0] == "get_accounts" and tools.count("get_accounts") == 6


async def test_reader_accounts_drops_non_mcp():
    _, _, spy = make()
    accts = await reads.Reader(spy).accounts()
    assert len(accts) == 1 and OTHER_ACCT not in json.dumps(accts) and MCP_ACCT not in json.dumps(accts)


@pytest.mark.parametrize("n_mcp", [0, 2])
async def test_zero_or_two_mcp_accounts_is_unknown(n_mcp):
    accounts = [{"account_number": f"A{i}", "agentic_allowed": True} for i in range(n_mcp)]
    accounts.append({"account_number": OTHER_ACCT, "agentic_allowed": False})
    _, _, spy = make(accounts)
    with pytest.raises(reads.Unknown):
        await reads.Reader(spy).positions()
    assert [t for t, _ in spy.seen] == ["get_accounts"]          # no data read without a single resolved account


async def test_agentic_flag_must_be_literally_true():
    accounts = [{"account_number": "A1", "agentic_allowed": "true"}, {"account_number": "A2", "agentic_allowed": 1}]
    _, _, spy = make(accounts)
    with pytest.raises(reads.Unknown):
        await reads.Reader(spy).positions()


async def test_realized_pnl_and_setting():
    _, _, spy = make()
    r = reads.Reader(spy)
    assert (await r.realized_pnl())["realized"] == 12.5
    assert await r.approval_setting() == {"human_must_approve": True}
    assert (await r.approvals())[0]["status"] == "pending"


# ---- state -------------------------------------------------------------------------------------------------------
def runtime(spy, **kw):
    return rh_state.Runtime(reads.Reader(spy), token_present=lambda: True, **kw)


async def test_poll_fills_state_and_leaks_no_other_account():
    _, _, spy = make()
    rt = runtime(spy, market_quiet=lambda: False)
    assert await rt.poll_once()
    s = rt.state.snapshot()
    assert s["enabled"] and s["connected"] and s["ok"] and s["unknown"] == []
    assert [p["symbol"] for p in s["positions"]] == ["AAA", "BBB", "CCC", "AAA", "BBB"]
    assert s["approvals"][0]["status"] == "pending" and s["as_of"]
    blob = json.dumps(s) + repr(rt) + repr(rt.state)
    assert OTHER_ACCT not in blob and MCP_ACCT not in blob and "personal" not in blob


async def test_two_mcp_accounts_state_reads_unknown():
    accounts = [{"account_number": "A1", "agentic_allowed": True}, {"account_number": "A2", "agentic_allowed": True}]
    _, _, spy = make(accounts)
    rt = runtime(spy, market_quiet=lambda: False)
    await rt.poll_once()
    s = rt.state.snapshot()
    assert s["ok"] is False and "account" in s["unknown"] and s["positions"] is None and s["orders"] is None


async def test_read_failure_marks_section_unknown_not_empty():
    _, st, spy = make()
    rt = runtime(spy, market_quiet=lambda: False)
    await rt.poll_once()
    st.fault = "500"
    await rt.poll_once()
    s = rt.state.snapshot()
    assert s["ok"] is False and s["positions"] is None and s["error"] in ("unreachable", "bad_reply", "unauthorized")
    assert "SECRET-TEXT" not in json.dumps(s)


async def test_disabled_and_no_token_make_no_request():
    t = CountingTransport()
    c = Client("http://127.0.0.1/mcp", Provider(dev_robinhood.make_app().state.mock), transport=t)
    rt = rh_state.Runtime(reads.Reader(c), token_present=lambda: False, market_quiet=lambda: False)
    assert await rt.poll_once() is False
    assert rt.state.snapshot() == {"enabled": True, "connected": False}
    assert t.n == 0


async def test_weekend_boot_with_token_and_no_snapshot_polls():
    _, _, spy = make()
    rt = runtime(spy, market_quiet=lambda: True)
    assert await rt.poll_once() is True and len(spy.seen) > 0      # no snapshot yet: unknown is not quiet


async def test_weekend_quiet_makes_no_request_when_provably_flat():
    _, st, spy = make()
    open_mkt = [False]
    rt = runtime(spy, market_quiet=lambda: open_mkt[0])
    await rt.poll_once()
    rt.state.data["orders"], rt.state.data["approvals"] = [], []   # a fully read, flat snapshot
    open_mkt[0] = True
    n = len(spy.seen)
    assert await rt.poll_once() is False and len(spy.seen) == n


async def test_weekend_unknown_section_keeps_polling():
    _, _, spy = make()
    open_mkt = [False]
    rt = runtime(spy, market_quiet=lambda: open_mkt[0])
    await rt.poll_once()
    rt.state.data["orders"], rt.state.data["approvals"], rt.state.data["pnl"] = [], [], None
    open_mkt[0] = True
    n = len(spy.seen)
    assert await rt.poll_once() is True and len(spy.seen) > n


async def test_weekend_failed_poll_keeps_polling():
    _, _, spy = make([{"account_number": "A1", "agentic_allowed": True}, {"account_number": "A2", "agentic_allowed": True}])
    rt = runtime(spy, market_quiet=lambda: True)
    assert await rt.poll_once() is True
    assert await rt.poll_once() is True


async def test_weekend_still_polls_when_not_flat():
    _, _, spy = make()
    open_mkt = [False]
    rt = runtime(spy, market_quiet=lambda: open_mkt[0])
    await rt.poll_once()                                   # snapshot has a queued order and a pending approval
    open_mkt[0] = True
    n = len(spy.seen)
    assert await rt.poll_once() is True and len(spy.seen) > n


def test_poll_interval_floor():
    assert rh_state.poll_interval({}) == 30.0
    assert rh_state.poll_interval({"PX_RH_POLL_S": "1"}) == 10.0
    assert rh_state.poll_interval({"PX_RH_POLL_S": "junk"}) == 30.0
    assert rh_state.poll_interval({"PX_RH_POLL_S": "45"}) == 45.0


async def test_stream_mode_hides_dollars():
    _, _, spy = make()
    rt = runtime(spy, market_quiet=lambda: False)
    await rt.poll_once()
    full, strm = rt.state.ai_block(stream=False), rt.state.ai_block(stream=True)
    assert "avg_price" in json.dumps(full) and "realized" in json.dumps(full)
    s = json.dumps(strm)
    assert "avg_price" not in s and "realized" not in s and "price" not in s and strm["stream"] is True
    assert strm["positions"][0]["symbol"] == "AAA" and strm["positions"][0]["qty"] == 10.0


def test_env_gate_and_defaults(tmp_path):
    assert rh_state.from_env({}) is None and rh_state.from_env({"PX_RH": "0"}) is None
    rt = rh_state.from_env({"PX_RH": "1", "PX_RH_TOKEN_FILE": str(tmp_path / "t.json")})
    assert rt is not None and rt.reader.client.base_url == "https://agent.robinhood.com/mcp/trading"
    assert rt.state.snapshot() == {"enabled": True, "connected": False}
    with pytest.raises(Exception):
        rh_state.from_env({"PX_RH": "1", "PX_RH_URL": "http://evil.example/mcp"})


async def test_one_poll_makes_exactly_one_get_accounts_call():
    _, _, spy = make()
    rt = runtime(spy, market_quiet=lambda: False)
    await rt.poll_once()
    assert [t for t, _ in spy.seen].count("get_accounts") == 1
    assert rt.state.snapshot()["ok"] is True
    await rt.poll_once()                                   # a fresh poll re-resolves
    assert [t for t, _ in spy.seen].count("get_accounts") == 2


async def test_failed_resolution_in_a_poll_calls_no_data_tool():
    accounts = [{"account_number": "A1", "agentic_allowed": True}, {"account_number": "A2", "agentic_allowed": True}]
    _, _, spy = make(accounts)
    rt = runtime(spy, market_quiet=lambda: False)
    await rt.poll_once()
    assert [t for t, _ in spy.seen] == ["get_accounts"]
    assert rt.state.snapshot()["error"] == "account_unknown"
