"""Robinhood phase 1, task 1: the order guard core against the mock. Money path: every refusal, single use, UNKNOWN."""
import ast
import json
import pathlib

import httpx
import pytest

from projectx_desk import dev_robinhood
from projectx_desk.rh import mcp as rh_mcp
from projectx_desk.rh.guard import Guard, Refused
from projectx_desk.rh.limits import RhLimits
from projectx_desk.rh.mcp import Client, McpError
from projectx_desk.rh.reads import Reader
from tests.test_rh_reads import MCP_ACCT, OTHER_ACCT, Provider

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = {"PX_RH_TRADING": "1", "PX_RH_MAX_NOTIONAL": "5000", "PX_RH_MAX_QTY": "50"}


class Rig:
    def __init__(self, tmp_path, env=None, accounts=None, clock=None):
        self.app = dev_robinhood.make_app(accounts)
        self.st = self.app.state.mock
        self.client = Client("http://127.0.0.1/mcp", Provider(self.st), transport=httpx.ASGITransport(app=self.app))
        self.client._limiter.n = 10_000          # the 4 calls/s limiter is covered in test_rh_mcp; do not sleep here
        self.reader = Reader(self.client)
        self.paths = dict(audit_path=tmp_path / "rh_audit.jsonl", halt_path=tmp_path / "rh_halt",
                          state_path=tmp_path / "rh_state.json")
        self.env = dict(ENV if env is None else env)
        self.clock = clock
        self.guard = self.make_guard()

    def make_guard(self):
        kw = {"clock": self.clock} if self.clock else {}
        return Guard(self.reader, self.client, RhLimits.from_env(self.env), market_open=lambda: True, **self.paths, **kw)

    def audit(self):
        p = self.paths["audit_path"]
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def buy(**kw):
    d = {"symbol": "DDD", "side": "buy", "qty": 2, "limit_price": 101.25}
    d.update(kw)
    return d


# ---- limits ------------------------------------------------------------------------------------------------------
def test_limits_defaults_fail_closed():
    lim = RhLimits.from_env({})
    assert lim.trading is False and lim.max_notional is None and lim.max_qty is None
    assert lim.ticket_ttl_s == 120 and lim.quote_max_age_s == 5


@pytest.mark.parametrize("bad", ["", "0", "-5", "abc", "nan", "inf", "  "])
def test_limits_zero_blank_garbled_money_and_qty_are_none(bad):
    lim = RhLimits.from_env({"PX_RH_TRADING": "1", "PX_RH_MAX_NOTIONAL": bad, "PX_RH_MAX_QTY": bad})
    assert lim.max_notional is None and lim.max_qty is None


def test_limits_trading_garbled_is_off_and_values_parse():
    assert RhLimits.from_env({"PX_RH_TRADING": "maybe"}).trading is False
    lim = RhLimits.from_env({"PX_RH_TRADING": "1", "PX_RH_MAX_NOTIONAL": "2500.5", "PX_RH_MAX_QTY": "7"})
    assert lim.trading and lim.max_notional == 2500.5 and lim.max_qty == 7


# ---- preview -----------------------------------------------------------------------------------------------------
async def test_preview_ok_makes_a_hashed_ticket_and_writes_nothing(rig):
    t = await rig.guard.preview(buy())
    v = t.view()
    assert t.state == "previewed" and len(t.hash8) == 8 and t.expires_at > 0
    assert v["symbol"] == "DDD" and v["side"] == "buy" and v["qty"] == 2 and v["state"] == "previewed"
    assert "fill_unprotected" in v["warnings"] and "no_paper_trading" in v["warnings"]
    assert MCP_ACCT not in json.dumps(v)
    assert not rig.st.place_calls and not any(c.endswith("_order") for c in rig.st.calls if c != "get_equity_orders")


async def refuse(rig, code, intent=None, **kw):
    with pytest.raises(Refused) as e:
        await rig.guard.preview(intent or buy(), **kw)
    assert e.value.code == code, e.value.code
    assert MCP_ACCT not in str(e.value) and MCP_ACCT not in e.value.detail


async def test_refuse_trading_off(tmp_path):
    await refuse(Rig(tmp_path, env={**ENV, "PX_RH_TRADING": "0"}), "trading_off")


async def test_refuse_halted(rig):
    rig.guard.trip_halt("manual")
    await refuse(rig, "halted")


@pytest.mark.parametrize("env", [{"PX_RH_MAX_NOTIONAL": ""}, {"PX_RH_MAX_QTY": "0"}, {"PX_RH_MAX_NOTIONAL": "0"}])
async def test_refuse_cap_unset(tmp_path, env):
    await refuse(Rig(tmp_path, env={**ENV, **env}), "cap_unset")


@pytest.mark.parametrize("intent", [
    buy(symbol="dd d"), buy(symbol=""), buy(symbol=None), buy(side="short"), buy(side=None), buy(qty=0), buy(qty=-1),
    buy(qty=1.5), buy(qty="3"), buy(qty=True), buy(qty=None), buy(limit_price=0), buy(limit_price=-2),
    buy(limit_price=float("nan")), buy(limit_price=float("inf")), buy(limit_price="x"), buy(limit_price=True),
    buy(limit_price=None), "not a dict", None])
async def test_refuse_bad_intent(rig, intent):
    with pytest.raises(Refused) as e:
        await rig.guard.preview(intent)
    assert e.value.code == "bad_intent"
    assert not rig.st.calls


async def test_refuse_qty_cap(rig):
    await refuse(rig, "qty_cap", buy(qty=51, limit_price=1))


async def test_refuse_notional_cap(rig):
    await refuse(rig, "notional_cap", buy(qty=50, limit_price=101.25))


async def test_notional_cap_counts_live_tickets(tmp_path):
    r = Rig(tmp_path, env={**ENV, "PX_RH_MAX_NOTIONAL": "300"})
    await r.guard.preview(buy(symbol="DDD", qty=2, limit_price=100))            # 200 reserved
    await refuse(r, "notional_cap", buy(symbol="EEE", qty=2, limit_price=100))  # 200 + 200 > 300


async def test_refuse_account_unknown_zero_and_two(tmp_path):
    none = [{"account_number": "X1", "agentic_allowed": False, "state": "active"}]
    await refuse(Rig(tmp_path, accounts=none), "account_unknown")
    two = [dict(a, agentic_allowed=True) for a in dev_robinhood.DEFAULT_ACCOUNTS]
    await refuse(Rig(tmp_path, accounts=two), "account_unknown")


async def test_refuse_account_unreadable(rig):
    rig.st.fault = "500"
    await refuse(rig, "account_unknown")


async def test_refuse_approvals_off(rig):
    rig.st.setting_on = False
    await refuse(rig, "approvals_off")


async def test_refuse_approvals_unknown(rig):
    rig.st.setting_reply = "missing"
    await refuse(rig, "approvals_unknown")


async def test_refuse_market_closed(tmp_path):
    r = Rig(tmp_path)
    r.guard.market_open = lambda: False
    await refuse(r, "market_closed")


@pytest.mark.parametrize("knob", ["stale", "no_timestamp", "no_quote"])
async def test_refuse_quote_stale_or_unreadable(rig, knob):
    if knob == "stale":
        rig.st.quote_age_s = 60
    elif knob == "no_timestamp":
        rig.st.quote_ts = False
    else:
        rig.st.quote_reply = "empty"
    await refuse(rig, "quote_stale")


async def test_refuse_symbol_busy_pending_approval(rig):
    await refuse(rig, "symbol_busy", buy(symbol="AAA"))          # the mock shows a pending AAA approval


async def test_refuse_symbol_busy_working_order(rig):
    await refuse(rig, "symbol_busy", {"symbol": "BBB", "side": "sell", "qty": 1, "limit_price": 22})


async def test_refuse_symbol_busy_second_ticket(rig):
    await rig.guard.preview(buy())
    await refuse(rig, "symbol_busy", buy(qty=1))


async def test_a_discarded_ticket_frees_the_symbol(rig):
    t = await rig.guard.preview(buy())
    rig.guard.discard(t.ticket_id)
    await rig.guard.preview(buy(qty=1))


async def test_sell_to_close_rules(rig):
    sell = {"symbol": "CCC", "side": "sell", "qty": 3, "limit_price": 310}
    t = await rig.guard.preview(sell)
    assert t.state == "previewed"
    rig.guard.discard(t.ticket_id)
    await refuse(rig, "sell_exceeds_position", dict(sell, qty=4))
    await refuse(rig, "no_position", dict(sell, symbol="ZZZ"))


async def test_refuse_read_failed_on_positions(rig, monkeypatch):
    async def boom():
        raise McpError("unreachable")
    monkeypatch.setattr(rig.reader, "positions", boom)
    await refuse(rig, "read_failed", {"symbol": "CCC", "side": "sell", "qty": 1, "limit_price": 310})


async def test_refuse_read_failed_on_orders(rig, monkeypatch):
    async def boom():
        raise McpError("unreachable")
    monkeypatch.setattr(rig.reader, "orders", boom)
    await refuse(rig, "read_failed")


# ---- place -------------------------------------------------------------------------------------------------------
async def test_place_happy_awaiting_approval_and_single_use(rig):
    t = await rig.guard.preview(buy())
    res = await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "awaiting_approval" and res["state"] == "awaiting_approval"
    assert t.state == "awaiting_approval" and not rig.guard.halted()
    assert len(rig.st.place_calls) == 1
    sent = rig.st.place_calls[0]
    assert sent["symbol"] == "DDD" and sent["side"] == "buy" and sent["account_number"] == MCP_ACCT
    assert sent["ref_id"] and float(sent["limit_price"]) == 101.25 and float(sent["quantity"]) == 2
    with pytest.raises(Refused) as e:
        await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert e.value.code == "ticket_used" and len(rig.st.place_calls) == 1


async def test_place_unknown_ticket_and_hash_mismatch(rig):
    with pytest.raises(Refused) as e:
        await rig.guard.place("nope", confirm_hash="deadbeef")
    assert e.value.code == "ticket_unknown"
    t = await rig.guard.preview(buy())
    with pytest.raises(Refused) as e:
        await rig.guard.place(t.ticket_id, confirm_hash="00000000")
    assert e.value.code == "hash_mismatch" and t.state == "previewed" and not rig.st.place_calls


async def test_place_ticket_expiry(tmp_path):
    now = [1000.0]
    r = Rig(tmp_path, clock=lambda: now[0])
    r.st.quote_clock = lambda: now[0]
    t = await r.guard.preview(buy())
    now[0] += 121
    with pytest.raises(Refused) as e:
        await r.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert e.value.code == "ticket_expired" and not r.st.place_calls


async def test_approvals_flip_off_between_preview_and_place(rig):
    t = await rig.guard.preview(buy())
    rig.st.setting_on = False
    with pytest.raises(Refused) as e:
        await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert e.value.code == "approvals_off" and not rig.st.place_calls
    assert any(a.get("code") == "approvals_off" for a in rig.audit())
    assert t.state == "refused"                                   # a refused place consumes the ticket


async def test_kill_switch_and_halt_rechecked_at_place(rig):
    t = await rig.guard.preview(buy())
    rig.guard.trip_halt("manual")
    with pytest.raises(Refused) as e:
        await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert e.value.code == "halted" and not rig.st.place_calls
    rig.guard.clear_halt()
    t2 = await rig.guard.preview(buy(symbol="EEE"))
    rig.guard.limits = RhLimits.from_env({**ENV, "PX_RH_TRADING": "0"})
    with pytest.raises(Refused) as e:
        await rig.guard.place(t2.ticket_id, confirm_hash=t2.hash8)
    assert e.value.code == "trading_off"


async def test_other_ticket_for_same_symbol_appearing_before_place_refuses(rig):
    t = await rig.guard.preview(buy())
    rig.st.orders_extra.append({"id": "mock-eo-9", "symbol": "DDD", "side": "buy", "type": "limit",
                                "state": "queued", "quantity": "1", "price": "5"})
    with pytest.raises(Refused) as e:
        await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert e.value.code == "symbol_busy" and not rig.st.place_calls


async def test_transport_error_after_send_is_unknown_halts_and_never_resends(rig):
    rig.st.place_reply = "drop"                                   # the mock records the order, then answers 500
    t = await rig.guard.preview(buy())
    res = await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "unknown" and t.state == "unknown"
    assert rig.guard.halted() and rig.guard.halt_cause() == "place_unknown"
    assert len(rig.st.place_calls) == 1
    with pytest.raises(Refused):
        await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    with pytest.raises(Refused) as e:
        await rig.guard.preview(buy(symbol="EEE"))
    assert e.value.code == "halted" and len(rig.st.place_calls) == 1


@pytest.mark.parametrize("reply", ["badjson", "no_state"])
async def test_unreadable_reply_is_unknown_and_halts(rig, reply):
    rig.st.place_reply = reply
    t = await rig.guard.preview(buy())
    res = await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "unknown" and rig.guard.halted() and len(rig.st.place_calls) == 1


async def test_live_order_reply_is_exposure_and_halts(rig):
    rig.st.place_reply = "live"                                   # approvals not honoured: the order is working
    t = await rig.guard.preview(buy())
    res = await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "placed" and rig.guard.halted() and rig.guard.halt_cause() == "live_order"
    assert any(a["event"] == "halt" for a in rig.audit())


async def test_definitive_tool_error_is_rejected_without_halt(rig):
    rig.st.place_reply = "fail"
    t = await rig.guard.preview(buy())
    res = await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "rejected" and t.state == "rejected" and not rig.guard.halted()
    assert len(rig.st.place_calls) == 0                           # the mock refused before creating anything


async def test_mock_refuses_a_write_to_a_non_mcp_account(rig):
    rig.st.accounts[0]["agentic_allowed"] = False
    rig.st.accounts[1]["agentic_allowed"] = True
    rig.st.accounts[1]["account_number"] = OTHER_ACCT
    # direct protocol check: the mock itself refuses an order for an account that is not agentic
    assert rig.st.place_for(rig.st.accounts[0]["account_number"]) is False


async def test_halt_and_clear_are_persisted_and_human_only(rig):
    rig.guard.trip_halt("manual")
    assert rig.make_guard().halted() and rig.make_guard().halt_cause() == "manual"
    rig.paths["halt_path"].write_text("free text with $ and spaces!", encoding="utf-8")
    assert rig.make_guard().halt_cause() == "unknown"
    g = rig.make_guard()
    g.clear_halt()
    assert not g.halted() and not rig.paths["halt_path"].exists()


async def test_audit_has_no_account_number_or_free_text(rig):
    t = await rig.guard.preview(buy())
    await rig.guard.place(t.ticket_id, confirm_hash=t.hash8)
    rig.guard.trip_halt("not a safe word $$$")
    text = rig.paths["audit_path"].read_text()
    assert MCP_ACCT not in text and OTHER_ACCT not in text and "$$$" not in text
    events = [a["event"] for a in rig.audit()]
    assert "preview" in events and "place" in events and "ack" in events
    for a in rig.audit():
        assert set(a) <= {"ts", "event", "ticket", "symbol", "side", "qty", "limit_price", "state", "code", "order_id"}


# ---- the write capability ----------------------------------------------------------------------------------------
async def test_submit_needs_the_guard_key_and_never_reaches_the_wire_without_it(rig):
    before = rig.st.requests
    for key in (None, object(), "k"):
        with pytest.raises(McpError) as e:
            await rig.client.submit("place_equity_order", {}, _guard_key=key)
        assert e.value.code == "write_blocked"
    assert rig.st.requests == before


def test_the_key_cannot_be_minted_outside_the_guard_module():
    with pytest.raises(McpError) as e:
        rh_mcp.mint_write_cap(frozenset({"place_equity_order"}))
    assert e.value.code == "write_blocked"


async def test_a_real_key_does_not_open_read_tools_or_unlisted_tools(rig):
    from projectx_desk.rh import guard as rg
    key = rg._KEY
    for tool in ("get_accounts", "place_option_order", "exercise_option"):
        with pytest.raises(McpError) as e:
            await rig.client.submit(tool, {}, _guard_key=key)
        assert e.value.code == "write_blocked"


async def test_reads_still_refuse_write_tools(rig):
    with pytest.raises(McpError) as e:
        await rig.client.call("place_equity_order", {})
    assert e.value.code == "write_blocked"


def _names_used(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.alias):
            out.add(n.name)
    return out


def test_only_the_rh_guard_reaches_the_write_path_and_the_desk_mcp_has_no_rh_tool():
    pkg = ROOT / "projectx_desk"
    for f in pkg.rglob("*.py"):
        rel = f.relative_to(ROOT).as_posix()
        if rel in ("projectx_desk/rh/guard.py", "projectx_desk/rh/mcp.py"):
            continue
        used = _names_used(f)
        assert not ({"submit", "mint_write_cap", "_guard_key", "_WriteCap"} & used), rel
    desk_mcp = (pkg / "mcp.py").read_text(encoding="utf-8")
    assert "robinhood" not in desk_mcp.lower() and "rh." not in desk_mcp and "from .rh" not in desk_mcp
