"""Robinhood phase 1: cancel (guard-only write, never retried) and the human release of a stuck ticket."""
import json

import pytest

from projectx_desk.rh.guard import Refused
from tests.test_rh_approvals import placed, timed, unknown_ticket
from tests.test_rh_guard import buy


# ---- cancel ------------------------------------------------------------------------------------------------------
async def test_cancel_an_awaiting_ticket_sends_one_cancel_and_frees_the_symbol(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    res = await r.guard.cancel(t.ticket_id)
    assert res["outcome"] == "cancelled" and t.state == "cancelled"
    assert len(r.st.cancel_calls) == 1 and r.st.cancel_calls[0]["order_id"] == t.order_id
    assert not r.guard.halted()
    await r.guard.preview(buy())                                  # the symbol is free again (the mock order is cancelled too)


async def test_cancel_is_audited_without_account_or_provider_text(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    await r.guard.cancel(t.ticket_id)
    ev = [a for a in r.audit() if a["event"] == "cancel"]
    assert ev and ev[-1]["ticket"] == t.hash8
    txt = r.paths["audit_path"].read_text()
    assert "MOCK-MCP-0001" not in txt and "MOCK-PERSONAL-0002" not in txt and "SECRET" not in txt


async def test_cancel_still_works_while_halted(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    r.guard.trip_halt("manual")
    assert (await r.guard.cancel(t.ticket_id))["outcome"] == "cancelled" and r.guard.halted()


async def test_cancel_refusals(tmp_path):
    r, _ = timed(tmp_path)
    with pytest.raises(Refused) as e:
        await r.guard.cancel("nope")
    assert e.value.code == "ticket_unknown"
    pv = await r.guard.preview(buy())                             # never sent: nothing to cancel
    with pytest.raises(Refused) as e:
        await r.guard.cancel(pv.ticket_id)
    assert e.value.code == "bad_state"
    t = await placed(r, symbol="EEE")
    t.order_id = None
    with pytest.raises(Refused) as e:
        await r.guard.cancel(t.ticket_id)
    assert e.value.code == "no_order"
    assert not r.st.cancel_calls


async def test_cancel_transport_error_is_unknown_halts_and_is_never_retried(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    r.st.cancel_reply = "drop"
    res = await r.guard.cancel(t.ticket_id)
    assert res["outcome"] == "unknown" and t.state == "unknown" and r.guard.halted()
    assert r.guard.halt_cause() == "cancel_unknown" and len(r.st.cancel_calls) == 1
    assert "SECRET" not in json.dumps(res) and "SECRET" not in r.paths["audit_path"].read_text()


async def test_cancel_unreadable_reply_is_unknown(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    r.st.cancel_reply = "badjson"
    assert (await r.guard.cancel(t.ticket_id))["outcome"] == "unknown" and r.guard.halted()


async def test_cancel_rejected_by_the_broker_leaves_the_ticket_alone_without_halting(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    r.st.cancel_reply = "fail"
    res = await r.guard.cancel(t.ticket_id)
    assert res["outcome"] == "rejected" and t.state == "awaiting_approval" and not r.guard.halted()


async def test_cancel_of_an_unknown_ticket_is_refused_reconcile_first(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r)
    with pytest.raises(Refused) as e:
        await r.guard.cancel(t.ticket_id)
    assert e.value.code == "bad_state"


# ---- release -----------------------------------------------------------------------------------------------------
async def test_release_frees_a_ticket_whose_order_and_approval_vanished(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r, reply="lost")                     # never recorded at the broker
    res = await r.guard.release(t.ticket_id)
    assert res["state"] == "released" and t.state == "released"
    assert r.guard.halted()                                       # release never clears the halt
    r.guard.clear_halt()
    await r.guard.preview(buy())                                  # symbol and notional are free
    assert [a for a in r.audit() if a["event"] == "release"]


async def test_release_refuses_while_the_broker_still_shows_the_order_or_approval(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    with pytest.raises(Refused) as e:
        await r.guard.release(t.ticket_id)
    assert e.value.code == "still_live" and t.state == "awaiting_approval"
    r.st.approvals_extra.clear()                                  # order alone is enough to refuse
    with pytest.raises(Refused) as e:
        await r.guard.release(t.ticket_id)
    assert e.value.code == "still_live"
    r.st.orders_extra.clear()
    assert (await r.guard.release(t.ticket_id))["state"] == "released"


async def test_release_refuses_on_unreadable_rows_and_wrong_state(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    r.st.orders_extra.clear()
    r.st.approvals_extra.clear()
    r.st.fault = "500"
    with pytest.raises(Refused) as e:
        await r.guard.release(t.ticket_id)
    assert e.value.code == "read_failed" and t.state == "awaiting_approval"
    r.st.fault = None
    pv = await r.guard.preview(buy(symbol="EEE"))
    with pytest.raises(Refused) as e:
        await r.guard.release(pv.ticket_id)
    assert e.value.code == "bad_state"
    with pytest.raises(Refused) as e:
        await r.guard.release("nope")
    assert e.value.code == "ticket_unknown"


async def test_release_sends_nothing(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r, reply="lost")
    n = len(r.st.place_calls)
    await r.guard.release(t.ticket_id)
    assert len(r.st.place_calls) == n and not r.st.cancel_calls
