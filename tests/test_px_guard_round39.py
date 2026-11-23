"""Round 39: ticket lifecycle (two clocks, a hash miss from the own origin burns the ticket, the customTag form), the over-size stop flag and resize,
and the read-back of every fixed post-fill stop against the fill."""
import json
from decimal import Decimal
from datetime import timedelta

import pytest

from projectx_desk.digest import format_digest
from projectx_desk.guard import ORDER_MODIFY, ORDER_PLACE, TAG_RE, Guard
from test_px_digest import state as digest_state
from test_px_guard_place import CID, build, go, staged
from test_px_guard_preview import NOW
from test_px_guard_protect import build2, fill


def audits(g):
    return [json.loads(x) for x in g.limits.audit_path.read_text().splitlines()]


# ---- (a) ticket lifecycle -------------------------------------------------------------------------

async def test_wall_clock_expiry_fires_even_when_the_monotonic_clock_did_not_move(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    g.wall = lambda: NOW + timedelta(seconds=121)       # a suspended laptop: wall moved, monotonic did not
    live.quotes[CID]["_rx"] = clock()
    r = await go(g, t)
    assert not r.ok and r.state == "EXPIRED" and gw.writes == []


async def test_a_ticket_inside_both_clocks_is_not_expired_and_a_broken_wall_clock_fails_closed(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    assert not g._expired(t)
    g.wall = lambda: NOW + timedelta(seconds=30)
    assert not g._expired(t)

    def boom():
        raise RuntimeError("no clock")
    g.wall = boom
    assert g._expired(t)


async def test_a_wrong_confirm_hash_burns_the_ticket(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    r = await g.place(t.ticket_id, origin="ui", confirm_hash="00000000")
    assert not r.ok and t.state == "REJECTED"
    r2 = await go(g, t)                                  # the right hash no longer helps
    assert not r2.ok and gw.writes == []


async def test_a_foreign_origin_cannot_burn_a_staged_ticket(tmp_path):
    g, live, clock, gw = build(tmp_path)               # mcp_place off by default
    t = await staged(g)                                 # a UI ticket
    r = await g.place(t.ticket_id, origin="mcp", confirm_hash=t.hash8)
    assert not r.ok and t.state == "STAGED" and gw.writes == []
    r = await g.place(t.ticket_id, origin="mcp", confirm_hash="00000000")      # a wrong hash from the wrong origin
    assert not r.ok and t.state == "STAGED"
    assert (await go(g, t)).ok                          # the desk's own confirm still works


async def test_pre_send_refusals_leave_the_ticket_staged(tmp_path):
    g, live, clock, gw = build(tmp_path, trading=False)
    t = await staged(g)
    r = await g.place(t.ticket_id, origin="ui", confirm_hash=t.hash8)
    assert not r.ok and t.state == "STAGED" and gw.writes == []
    g2, live2, clock2, gw2 = build(tmp_path / "b")
    t2 = await staged(g2)
    assert not (await g2.place(t2.ticket_id, origin="bogus", confirm_hash=t2.hash8)).ok
    assert t2.state == "STAGED" and gw2.writes == []


async def test_custom_tag_has_one_fixed_form_and_a_bad_tag_is_never_sent(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    assert TAG_RE.fullmatch(t.custom_tag) and len(t.custom_tag) == 13 and t.payload["customTag"] == t.custom_tag
    for good in ("px-0123abcd-1", "px-0123abcd-2", "px-0123abcd-9"):
        assert TAG_RE.fullmatch(good)
    for bad in ("", "px-0123ABCD-1", "px-0123abc-1", "px-0123abcde-1", "px-0123abcd-12", "px-0123abcd-1\n", "xx-0123abcd-1",
                "px-0123abcd-1 ", "px-0123abcg-1", "px_0123abcd_1"):
        assert not TAG_RE.fullmatch(bad), bad
    t.custom_tag = "px-zzzz-1"
    r = await go(g, t)
    assert not r.ok and gw.writes == []


# ---- (c) fixed stop read-back ---------------------------------------------------------------------

STOPROW = {"id": 900, "accountId": 7, "contractId": CID, "type": 4, "side": 1, "size": 1, "stopPrice": 4994.0, "status": 1}


@pytest.mark.parametrize("over,close_side,want", [
    ({}, 1, None),
    ({"type": 5}, 1, "not_a_stop"),
    ({"status": 3}, 1, "not_working"),
    ({"side": 0}, 1, "wrong_side"),
    ({"size": 0}, 1, "too_small"),
    ({"stopPrice": None}, 1, "unreadable"),
    ({"stopPrice": float("nan")}, 1, "unreadable"),
    ({"stopPrice": 5010.0}, 1, "wrong_side_of_fill"),          # a long's stop above the fill (soft unless the market or plan says so)
    ({"stopPrice": 5000.25}, 1, "wrong_side_of_fill"),         # on the fill is not protection
    ({"stopPrice": 4900.0}, 1, "too_far"),                     # 401 ticks (soft: no planned entry to measure against)
    ({"side": 0, "stopPrice": 5006.0}, 0, None),
    ({"side": 0, "stopPrice": 4994.0}, 0, "wrong_side_of_fill"),
])
def test_fixed_stop_problem_table(over, close_side, want):
    got = {**STOPROW, **over}
    assert Guard._fixed_stop_problem(got, close_side, 1, 5000.25, 0.25, 200) == want


def test_fixed_stop_problem_unknown_inputs_are_problems_never_passes():
    assert Guard._fixed_stop_problem(None, 1, 1, 5000.25, 0.25, 200) == "not_a_stop"
    assert Guard._fixed_stop_problem(STOPROW, 1, None, 5000.25, 0.25, 200) == "too_small"
    assert Guard._fixed_stop_problem(STOPROW, 1, 1, None, 0.25, 200) == "unreadable"
    assert Guard._fixed_stop_problem(STOPROW, 1, 1, 5000.25, 0, 200) == "unreadable"
    assert Guard._fixed_stop_problem({**STOPROW, "stopPrice": 4900.0}, 1, 1, 5000.25, 0.25, None) is None   # one-lot skips only the distance


BOOK = (Decimal("4999.75"), Decimal("5000.25"))


@pytest.mark.parametrize("over,close_side,planned,want", [
    ({"stopPrice": 4994.0}, 1, 5000.0, None),
    ({"stopPrice": 5010.0}, 1, 5000.0, "wrong_side_of_plan"),           # over the planned entry
    ({"stopPrice": 4999.75}, 1, 5000.0, "wrong_side_of_market"),
    ({"side": 0, "stopPrice": 5000.25}, 0, 4999.0, "wrong_side_of_market"),
    ({"stopPrice": 4900.0}, 1, 5000.0, "too_far_plan"),                 # beyond the ceiling from the PLANNED entry too
    ({"stopPrice": 4950.25}, 1, 5000.0, None),                          # 199 ticks from the plan, and from the fill
])
def test_fixed_stop_problem_hard_cases_use_market_and_plan(over, close_side, planned, want):
    got = {**STOPROW, **over}
    assert Guard._fixed_stop_problem(got, close_side, 1, 5000.25, 0.25, 200, planned_px=planned, book=BOOK) == want


def test_a_stop_equal_to_the_plan_that_the_market_ran_through_is_drift_not_wrong():
    got = {**STOPROW, "stopPrice": 4999.75}
    kw = dict(planned_px=5000.0, book=BOOK)
    assert Guard._fixed_stop_problem(got, 1, 1, 5000.25, 0.25, 200, plan_stop=4999.75, **kw) == "market_through_plan"
    assert "market_through_plan" in Guard.SOFT_READBACK
    assert Guard._fixed_stop_problem(got, 1, 1, 5000.25, 0.25, 200, plan_stop=4994.0, **kw) == "wrong_side_of_market"   # differs from the plan
    assert Guard._fixed_stop_problem(got, 1, 1, 5000.25, 0.25, 200, **kw) == "wrong_side_of_market"


def test_slippage_alone_is_soft_not_a_flatten_reason():
    # planned entry 5000.0, stop 4950.0 = exactly 200 ticks from the plan; a 2-tick slip to a fill of 5000.5 makes it 202 from the fill
    got = {**STOPROW, "stopPrice": 4950.0}
    assert Guard._fixed_stop_problem(got, 1, 1, 5000.5, 0.25, 200, planned_px=5000.0, book=(Decimal("5000.25"), Decimal("5000.5"))) == "too_far"
    assert "too_far" in Guard.SOFT_READBACK and "wrong_side_of_fill" in Guard.SOFT_READBACK


async def test_a_matching_fixed_stop_passes_the_readback(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    gw.working = [dict(STOPROW)]
    await g.handle_position(fill(live))
    assert t.stop_state == "ACKED" and not g.halted()
    assert any(a.get("state") == "STOP_READBACK_OK" for a in audits(g))


async def test_a_stop_on_the_wrong_side_of_the_fill_flattens_and_halts(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    gw.working = [{**STOPROW, "stopPrice": 5010.0}]
    await g.handle_position(fill(live))
    assert g.halted() and t.abandoned
    assert any(a.get("state") == "STOP_READBACK_BAD" and a.get("code") in ("wrong_side_of_market", "wrong_side_of_plan") for a in audits(g))
    assert g.auto_action()["kind"] == "stop_readback"


async def test_a_slipped_fill_with_a_stop_near_the_ceiling_is_flagged_as_drift_never_flattened(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    ref = float(t.readback["entry_ref"])
    live.quotes[CID]["bestBid"], live.quotes[CID]["bestAsk"] = ref + 0.5, ref + 1.0
    live.quotes[CID]["_rx"] = clock()
    gw.working = [{**STOPROW, "stopPrice": 4994.0}]
    g.limits = g.limits.__class__(**{**g.limits.__dict__, "max_stop_ticks": int(round(abs(ref - 4994.0) / 0.25))})
    p = fill(live)
    p["averagePrice"] = ref + 0.5                       # 2 ticks of slippage: the fill-to-trigger distance is 2 ticks over the ceiling
    await g.handle_position(p)
    assert not g.halted() and not t.abandoned and t.stop_state == "ACKED"
    assert any(a.get("state") == "STOP_READBACK_DRIFT" and a.get("code") == "too_far" for a in audits(g))
    assert not any(a.get("state") == "STOP_READBACK_BAD" for a in audits(g))


async def test_a_position_that_grew_at_readback_is_resized_not_flattened(tmp_path):
    g, live, clock, gw = build2(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3)
    t = await staged(g, risk_dollars=200.0)
    await go(g, t)
    gw.working = [{**STOPROW, "size": 1}]
    real = g._read_order

    async def grow_then_read(oid):
        live.positions[1]["size"] = 3                   # the rest of the entry filled between the ACK and the read-back
        gw.working = [{**STOPROW, "size": 3}]           # the modify (resize) is what makes the broker row match
        return await real(oid)

    g._read_order = grow_then_read
    t.stop_state, t.stop_order_id, t.stop_size = "ACKED", 900, 1
    fill(live, size=1)
    await g._fixed_stop_readback(t, 900)
    mods = [b for p_, b in gw.writes if p_ == ORDER_MODIFY]
    assert mods and mods[-1]["size"] == 3 and mods[-1]["stopPrice"] == 4994.0
    assert t.stop_size == 3 and not g.halted() and not t.abandoned
    assert not any(a.get("state") == "STOP_READBACK_BAD" for a in audits(g))


async def test_a_resized_stop_the_broker_still_shows_too_small_flattens(tmp_path):
    g, live, clock, gw = build2(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3)
    t = await staged(g, risk_dollars=200.0)
    await go(g, t)
    t.stop_state, t.stop_order_id, t.stop_size = "ACKED", 900, 1
    fill(live, size=3)
    gw.working = [{**STOPROW, "size": 1}]               # the broker never took the resize
    await g._fixed_stop_readback(t, 900)
    assert g.halted() and any(a.get("state") == "STOP_READBACK_BAD" and a.get("code") == "too_small" for a in audits(g))


async def test_a_stop_that_never_lists_is_rechecked_then_flattened_like_a_trail(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    gw.list_acked = False                                  # ACKed with an id, never listed, position still open
    await g.handle_position(fill(live))
    assert g.halted() and t.abandoned
    assert any(a.get("state") == "STOP_READBACK_UNLISTED" for a in audits(g))
    assert g.auto_action()["kind"] == "stop_readback"


async def test_an_unlisted_stop_on_a_position_that_is_already_flat_does_nothing(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    t.stop_state, t.stop_order_id, t.stop_size = "ACKED", 900, 1
    gw.working = []
    live.positions.clear()                                 # the stop filled / a human closed it
    await g._fixed_stop_readback(t, 900)
    assert not g.halted() and not t.abandoned


async def test_a_list_that_lags_one_read_after_a_resize_does_not_flatten(tmp_path):
    g, live, clock, gw = build2(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3)
    t = await staged(g, risk_dollars=200.0)
    await go(g, t)
    t.stop_state, t.stop_order_id, t.stop_size = "ACKED", 900, 1
    fill(live, size=3)
    gw.working = [{**STOPROW, "size": 1}]               # the OLD size, as a stale list shows it
    calls = {"n": 0}
    real = gw.open_orders

    async def lagging(account_id):
        calls["n"] += 1
        if calls["n"] >= 3:                             # catches up after the resize and a couple of reads; gw.working is NOT rewritten
            return [{**STOPROW, "size": 3}]
        return await real(account_id)

    gw.open_orders = lagging
    await g._fixed_stop_readback(t, 900)
    assert calls["n"] >= 3 and not g.halted() and not t.abandoned
    assert any(a.get("state") == "STOP_READBACK_OK" for a in audits(g))


async def test_a_fill_price_the_desk_cannot_read_is_flagged_not_flattened(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    gw.working = [dict(STOPROW)]
    p = fill(live)
    p.pop("averagePrice")
    await g.handle_position(p)
    assert not g.halted() and any(a.get("state") == "STOP_READBACK_UNREAD" for a in audits(g))


# ---- (b) over-size stop ---------------------------------------------------------------------------

BIG = {"id": 951, "accountId": 7, "contractId": CID, "type": 4, "side": 1, "size": 3, "stopPrice": 4985.0, "status": 1}


async def test_an_external_oversize_stop_is_flagged_and_never_touched(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    fill(live, size=1)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    gw.working = [dict(BIG)]
    out = await g.protect_open_positions()
    assert out == {CID: "COVERED"} and g.oversize_flags() == [{"contract": CID, "stop": 3, "pos": 1}]
    assert not [w for w in gw.writes if w[0] in (ORDER_PLACE, ORDER_MODIFY)] and not g.halted()
    assert any(a.get("state") == "OVERSIZE_STOP" for a in audits(g))
    await g.protect_open_positions()                       # the same condition is audited once
    assert len([a for a in audits(g) if a.get("state") == "OVERSIZE_STOP"]) == 1
    gw.working = [{**BIG, "size": 1}]                      # fixed: the flag clears
    await g.protect_open_positions()
    assert g.oversize_flags() == []


@pytest.mark.parametrize("row", [{"size": 1}, {"side": 0}, {"status": 3}, {"type": 1}, {"contractId": "CON.F.US.MNQ.Z6"}, {"size": None}])
async def test_only_a_working_closing_side_stop_bigger_than_the_position_is_flagged(tmp_path, row):
    g, live, clock, gw = build2(tmp_path)
    fill(live, size=1)
    gw.working = [{**BIG, **row}]
    await g.protect_open_positions()
    assert g.oversize_flags() == []


async def test_an_owned_stop_bigger_than_the_position_is_resized_down_at_its_own_price(tmp_path):
    g, live, clock, gw = build2(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3)
    t = await staged(g, risk_dollars=200.0)
    await go(g, t)
    gw.working = [{**BIG, "id": 900, "size": 3, "stopPrice": 4994.0}]
    await g.handle_position(fill(live, size=3))
    assert t.stop_size == 3 and t.stop_order_id == 900
    live.positions[1]["size"] = 1                          # a reduction the events never delivered
    t.stop_size = 1                                        # the ticket believes it already matches; the broker shows 3
    await g.protect_open_positions()
    mods = [b for p, b in gw.writes if p == ORDER_MODIFY]
    assert mods and mods[-1]["size"] == 1 and mods[-1]["stopPrice"] == 4994.0 and mods[-1]["trailPrice"] is None
    assert g.oversize_flags() == []


async def test_an_oversize_trail_is_flagged_only_never_resized_or_flattened(tmp_path):
    g, live, clock, gw = build2(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3)
    t = await staged(g, risk_dollars=200.0)
    await go(g, t)
    fill(live, size=3)
    t.stop_state, t.stop_order_id, t.stop_size, t.trail, t.stop_current = "ACKED", 900, 3, True, 4994.0
    live.positions[1]["size"] = 1                       # the trader closed two; the trail still says 3
    t.stop_size = 1                                     # the ticket believes it matches (the case the new branch targets)
    gw.working = [{**BIG, "id": 900, "type": 5, "size": 3, "stopPrice": 4994.0}]
    n = len(gw.writes)
    await g.protect_open_positions()
    assert not [w for w in gw.writes[n:] if w[0] == ORDER_MODIFY] and not g.halted() and not t.abandoned
    assert g.oversize_flags() == [{"contract": CID, "stop": 3, "pos": 1}]


async def test_oversize_seen_is_pruned_to_contracts_with_a_position_and_capped(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    fill(live, size=1)
    gw.working = [dict(BIG)]
    await g.protect_open_positions()
    assert g._oversize_seen
    live.positions.clear()
    gw.pos = []
    await g.protect_open_positions()
    assert g._oversize_seen == set()
    g._oversize_seen = {(CID, i, 3, 1) for i in range(300)}
    fill(live, size=1)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    await g.protect_open_positions()
    assert len(g._oversize_seen) <= 200


async def test_ticket_ttl_shown_reads_both_clocks(tmp_path):
    g, live, clock, gw = build(tmp_path)
    t = await staged(g)
    assert g.ttl_s(t) > 0
    g.wall = lambda: NOW + timedelta(seconds=121)
    assert g._expired(t) and g.ttl_s(t) == 0


def test_the_digest_shows_the_oversize_flag_and_is_quiet_without_it():
    s = digest_state()
    assert "OVERSIZE_STOP" not in format_digest(s)
    s["mode"] = {**s["mode"], "oversize_stops": [{"contract": CID, "stop": 3, "pos": 1}]}
    assert f"ALERT: OVERSIZE_STOP contract={CID} stop_size=3 position=1" in format_digest(s)


def test_the_oversize_alert_survives_stream_mode_with_no_dollar_or_account_text():
    from projectx_desk.digest import redact_stream
    s = digest_state()
    s["mode"] = {**s["mode"], "oversize_stops": [{"contract": CID, "stop": 3, "pos": 1}]}
    out = format_digest(redact_stream(s))
    line = next(x for x in out.splitlines() if x.startswith("ALERT: OVERSIZE_STOP"))
    assert line == f"ALERT: OVERSIZE_STOP contract={CID} stop_size=3 position=1 (a stop fill would flip the position)"
    assert "$" not in line
