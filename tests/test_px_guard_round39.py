"""Round 39: ticket lifecycle (two clocks, a failed attempt burns the ticket, the customTag form), the over-size stop flag and resize,
and the read-back of every fixed post-fill stop against the fill."""
import json
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


async def test_other_refused_attempts_burn_the_ticket_too(tmp_path):
    g, live, clock, gw = build(tmp_path, trading=False)
    t = await staged(g)
    r = await g.place(t.ticket_id, origin="ui", confirm_hash=t.hash8)
    assert not r.ok and t.state == "REJECTED" and gw.writes == []
    g2, live2, clock2, gw2 = build(tmp_path / "b")
    t2 = await staged(g2)
    assert not (await g2.place(t2.ticket_id, origin="bogus", confirm_hash=t2.hash8)).ok
    assert t2.state == "REJECTED" and gw2.writes == []


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
    ({"stopPrice": 5010.0}, 1, "wrong_side_of_fill"),          # a long's stop above the fill
    ({"stopPrice": 5000.25}, 1, "wrong_side_of_fill"),         # on the fill is not protection
    ({"stopPrice": 4900.0}, 1, "too_far"),                     # 401 ticks
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
    assert any(a.get("state") == "STOP_READBACK_BAD" and a.get("code") == "wrong_side_of_fill" for a in audits(g))
    assert g.auto_action()["kind"] == "stop_readback"


async def test_an_unlisted_stop_is_audited_and_an_undersized_one_halts(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    t = await staged(g)
    await go(g, t)
    gw.working = []                                        # not listed: cannot be read back, so audited, not guessed
    await g.handle_position(fill(live))
    assert not g.halted()
    assert any(a.get("state") == "STOP_READBACK_UNREAD" for a in audits(g))
    g2, live2, clock2, gw2 = build2(tmp_path / "b")
    t2 = await staged(g2)
    await go(g2, t2)
    gw2.working = [{**STOPROW, "size": 0}]
    await g2.handle_position(fill(live2))
    assert g2.halted()


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


def test_the_digest_shows_the_oversize_flag_and_is_quiet_without_it():
    s = digest_state()
    assert "OVERSIZE_STOP" not in format_digest(s)
    s["mode"] = {**s["mode"], "oversize_stops": [{"contract": CID, "stop": 3, "pos": 1}]}
    assert f"ALERT: OVERSIZE_STOP contract={CID} stop_size=3 position=1" in format_digest(s)
