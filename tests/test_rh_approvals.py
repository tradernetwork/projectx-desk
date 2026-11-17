"""Robinhood phase 1, task 2: the approval state machine, reconcile, caps freeing and restart persistence."""
import json

import pytest

from projectx_desk.rh import state as rh_state
from projectx_desk.rh.guard import Guard, Refused
from projectx_desk.rh.limits import RhLimits
from tests.test_rh_guard import ENV, Rig, buy


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def timed(tmp_path, env=None):
    clk = Clock()
    r = Rig(tmp_path, env=env, clock=clk)
    r.st.quote_clock = clk
    return r, clk


async def placed(r, **kw):
    t = await r.guard.preview(buy(**kw))
    res = await r.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "awaiting_approval"
    return t


# ---- transitions -------------------------------------------------------------------------------------------------
async def test_pending_approval_keeps_the_ticket_awaiting(tmp_path):
    r, clk = timed(tmp_path)
    t = await placed(r)
    clk.t += 16
    assert await r.guard.track() is True
    assert t.state == "awaiting_approval"


async def test_approved_then_filled(tmp_path):
    r, clk = timed(tmp_path)
    t = await placed(r)
    r.st.approve(t.ref_id)
    clk.t += 16
    await r.guard.track()
    assert t.state == "approved"
    r.st.fill(t.ref_id)
    clk.t += 16
    await r.guard.track()
    assert t.state == "filled"
    clk.t += 16
    assert await r.guard.track() is False                    # nothing left to watch: no read


@pytest.mark.parametrize("action,expected", [("decline", "declined"), ("expire", "expired")])
async def test_declined_or_expired_frees_the_symbol_and_the_notional(tmp_path, action, expected):
    r, clk = timed(tmp_path, env={**ENV, "PX_RH_MAX_NOTIONAL": "300"})
    t = await placed(r, qty=2, limit_price=100)             # 200 of 300 reserved
    with pytest.raises(Refused) as e:
        await r.guard.preview(buy(symbol="EEE", qty=2, limit_price=100))
    assert e.value.code == "notional_cap"
    with pytest.raises(Refused) as e:
        await r.guard.preview(buy(qty=1, limit_price=100))
    assert e.value.code == "symbol_busy"
    getattr(r.st, action)(t.ref_id)
    clk.t += 16
    await r.guard.track()
    assert t.state == expected
    await r.guard.preview(buy(qty=1, limit_price=100))      # same symbol and the freed budget work again
    await r.guard.preview(buy(symbol="EEE", qty=1, limit_price=100))


async def test_awaiting_counts_toward_the_caps_until_it_resolves(tmp_path):
    r, clk = timed(tmp_path, env={**ENV, "PX_RH_MAX_NOTIONAL": "300"})
    await placed(r, qty=2, limit_price=100)
    clk.t += 16
    await r.guard.track()
    with pytest.raises(Refused) as e:
        await r.guard.preview(buy(symbol="EEE", qty=2, limit_price=100))
    assert e.value.code == "notional_cap"


async def test_an_order_cancelled_after_approval_reads_as_declined(tmp_path):
    r, clk = timed(tmp_path)
    t = await placed(r)
    r.st.approve(t.ref_id)
    clk.t += 16
    await r.guard.track()
    for o in r.st.orders_extra:
        o["state"] = "cancelled"
    clk.t += 16
    await r.guard.track()
    assert t.state == "declined"


# ---- cadence and backoff -----------------------------------------------------------------------------------------
async def test_track_polls_every_15s_and_backs_off_on_errors_to_60s(tmp_path):
    r, clk = timed(tmp_path)
    t = await placed(r)
    assert await r.guard.track() is False                    # due 15 s after the place, not now
    clk.t += 15
    n0 = r.st.requests
    assert await r.guard.track() is True and r.st.requests > n0
    assert await r.guard.track() is False                    # just polled
    r.st.fault = "500"
    gaps = []
    for _ in range(4):
        before = r.guard._next_track
        clk.t = max(clk.t, before)
        assert await r.guard.track() is False                # a failed poll
        gaps.append(r.guard._next_track - clk.t)
    assert gaps == [30, 60, 60, 60]
    r.st.fault = None
    clk.t = r.guard._next_track
    assert await r.guard.track() is True
    assert r.guard._next_track - clk.t == 15                 # success resets the cadence
    assert t.state == "awaiting_approval"


async def test_track_with_nothing_awaiting_makes_no_call(tmp_path):
    r, clk = timed(tmp_path)
    await r.guard.preview(buy())                             # previewed only
    clk.t += 100
    n0 = r.st.requests
    assert await r.guard.track() is False and r.st.requests == n0


async def test_track_still_runs_while_halted(tmp_path):
    r, clk = timed(tmp_path)
    t = await placed(r)
    r.guard.trip_halt("manual")
    r.st.decline(t.ref_id)
    clk.t += 16
    await r.guard.track()
    assert t.state == "declined" and r.guard.halted()


# ---- reconcile ---------------------------------------------------------------------------------------------------
async def unknown_ticket(r, reply="drop"):
    r.st.place_reply = reply
    t = await r.guard.preview(buy())
    res = await r.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert res["outcome"] == "unknown"
    r.st.place_reply = None
    return t


async def test_reconcile_finds_the_order_by_ref_id(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r)
    n = r.st.calls.count("place_equity_order")
    res = await r.guard.reconcile(t.ticket_id)
    assert res["state"] == "awaiting_approval" and t.state == "awaiting_approval"
    assert r.guard.halted()                                  # reconcile never clears the halt
    assert r.st.calls.count("place_equity_order") == n       # and never resends


async def test_reconcile_not_found_stays_unknown_and_never_resends(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r, reply="lost")                # the mock never recorded it
    res = await r.guard.reconcile(t.ticket_id)
    assert res["state"] == "unknown" and t.state == "unknown"
    assert r.st.calls.count("place_equity_order") == 1 and r.guard.halted()


async def test_reconcile_with_unreadable_orders_stays_unknown(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r)
    r.st.fault = "500"
    res = await r.guard.reconcile(t.ticket_id)
    assert res["state"] == "unknown" and t.state == "unknown"


async def test_reconcile_found_filled_and_found_live(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r)
    r.st.approve(t.ref_id, fill=True)
    assert (await r.guard.reconcile(t.ticket_id))["state"] == "filled"
    r2, _ = timed(tmp_path / "b")
    t2 = await unknown_ticket(r2)
    r2.st.approvals_extra.clear()                            # a working order with no approval record: exposure
    r2.st.orders_extra[0]["state"] = "queued"
    assert (await r2.guard.reconcile(t2.ticket_id))["state"] == "placed"
    assert r2.guard.halted()


async def test_reconcile_ignores_tickets_that_are_not_unknown(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    n = r.st.requests
    res = await r.guard.reconcile(t.ticket_id)
    assert res["state"] == "awaiting_approval" and r.st.requests == n
    with pytest.raises(Refused) as e:
        await r.guard.reconcile("nope")
    assert e.value.code == "ticket_unknown"


# ---- restart ---------------------------------------------------------------------------------------------------
async def test_halt_and_an_unknown_ticket_survive_a_restart(tmp_path):
    r, _ = timed(tmp_path)
    t = await unknown_ticket(r, reply="lost")
    g2 = r.make_guard()
    assert g2.halted() and g2.halt_cause() == "place_unknown"
    t2 = g2.tickets[t.ticket_id]
    assert t2.state == "unknown" and t2.ref_id == t.ref_id and t2.hash8 == t.hash8
    g2.clear_halt()                                          # a human clears the halt; the UNKNOWN ticket still holds the symbol
    with pytest.raises(Refused) as e:
        await g2.preview(buy())
    assert e.value.code == "symbol_busy"
    assert (await g2.reconcile(t.ticket_id))["state"] == "unknown"


async def test_a_ticket_caught_mid_send_by_a_crash_loads_as_unknown_and_halts(tmp_path):
    r, _ = timed(tmp_path)
    t = await placed(r)
    p = r.paths["state_path"]
    d = json.loads(p.read_text())
    d["tickets"][t.ticket_id]["state"] = "sending"
    p.write_text(json.dumps(d))
    g2 = r.make_guard()
    assert g2.tickets[t.ticket_id].state == "unknown" and g2.halted()


async def test_a_corrupt_state_file_halts(tmp_path):
    r, _ = timed(tmp_path)
    r.paths["state_path"].write_text("{not json", encoding="utf-8")
    g2 = r.make_guard()
    assert g2.halted() and g2.halt_cause() == "state_unreadable"


async def test_state_file_failure_at_the_commit_point_refuses_before_any_send(tmp_path):
    r, _ = timed(tmp_path)
    t = await r.guard.preview(buy())
    r.guard.state_path = tmp_path                            # a directory: cannot be written
    with pytest.raises(Refused) as e:
        await r.guard.place(t.ticket_id, confirm_hash=t.hash8)
    assert e.value.code == "state_unwritable" and not r.st.place_calls


async def test_state_file_has_no_account_number(tmp_path):
    r, _ = timed(tmp_path)
    await placed(r)
    txt = r.paths["state_path"].read_text()
    assert "MOCK-MCP-0001" not in txt and "MOCK-PERSONAL-0002" not in txt


# ---- the snapshot ------------------------------------------------------------------------------------------------
async def test_snapshot_gains_tickets_and_halt(tmp_path):
    r, _ = timed(tmp_path)
    rt = rh_state.Runtime(r.reader)
    rt.attach_guard(r.guard)
    t = await placed(r)
    await rt.poll_once()
    snap = rt.state.snapshot()
    assert [x["ticket_id"] for x in snap["tickets"]] == [t.ticket_id] and snap["halt"] is None
    assert "ref_id" not in json.dumps(snap["tickets"]) and t.ref_id not in json.dumps(snap)
    r.guard.trip_halt("manual")
    assert rt.state.snapshot()["halt"] == "manual"
    s = rt.state.snapshot(stream=True)
    assert all("limit_price" not in x and "notional" not in x for x in s["tickets"])


async def test_snapshot_without_a_guard_is_unchanged(tmp_path):
    r, _ = timed(tmp_path)
    rt = rh_state.Runtime(r.reader)
    await rt.poll_once()
    snap = rt.state.snapshot()
    assert "tickets" not in snap and "halt" not in snap
