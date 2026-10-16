"""Topstep rule enforcement: the 3:10 PM CT flat-by deadline and the live maximum-loss-limit floor.
Rules and sources: docs/TOPSTEP_RULES.md."""
import pytest

from projectx_desk.guard import POSITION_CLOSE
from test_px_guard_place import CID, staged  # noqa: F401
from test_px_guard_protect import build2, fill
from test_px_guard_preview import NOW   # 15:00 UTC = 10:00 CT


def failed(p):
    return {n for n, ok, _ in p.verdict if not ok}


async def prev(g, **kw):
    args = dict(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=50.0)
    args.update(kw)
    return await g.preview(**args)


# ---- flat by ---------------------------------------------------------------------------------------

async def test_no_new_entry_in_the_last_two_minutes_before_flat_by(tmp_path):
    g, live, clock, gw = build2(tmp_path, flat_by="10:01")          # the fixture clock is 10:00 CT
    p = await prev(g)
    assert not p.ok and "session_window" in failed(p)


async def test_entries_are_fine_well_before_flat_by(tmp_path):
    g, live, clock, gw = build2(tmp_path, flat_by="15:10")
    assert (await prev(g)).ok


async def test_a_garbled_flat_by_refuses_every_open(tmp_path):
    g, live, clock, gw = build2(tmp_path, flat_by="3:10pm")
    assert "session_window" in failed(await prev(g))


async def test_flat_by_flattens_an_open_position_at_the_deadline(tmp_path):
    g, live, clock, gw = build2(tmp_path, flat_by="09:59")           # already past it at 10:00 CT
    fill(live)
    gw.pos = [{"id": 1, "contractId": CID, "type": 1, "size": 1}]
    r = await g.enforce_flat_by()
    assert r is not None and r.flat and gw.pos == [] and any(p == POSITION_CLOSE for p, _ in gw.writes)


async def test_flat_by_also_cancels_a_resting_entry(tmp_path):
    g, live, clock, gw = build2(tmp_path, flat_by="09:59")
    live.orders[3] = {"id": 3, "accountId": 7, "contractId": CID, "status": 1}
    gw.working = [{"id": 3, "contractId": CID}]
    r = await g.enforce_flat_by()
    assert r is not None and gw.cancelled == [3]


async def test_flat_by_does_nothing_before_the_deadline_or_when_flat(tmp_path):
    g, live, clock, gw = build2(tmp_path, flat_by="15:10")
    fill(live)
    assert await g.enforce_flat_by() is None and gw.writes == []
    g2, live2, _, gw2 = build2(tmp_path, flat_by="09:59")
    assert await g2.enforce_flat_by() is None and gw2.writes == []


async def test_flat_by_unset_never_flattens(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    fill(live)
    assert not g.flat_by_due() and await g.enforce_flat_by() is None


# ---- live maximum loss limit floor -------------------------------------------------------------------

async def test_drawdown_room_follows_the_live_balance_above_the_floor(tmp_path):
    g, live, clock, gw = build2(tmp_path, mll_floor=48000.0, dd_buffer=0.0)
    live.account["balance"] = 50400.0
    assert g.dd_room() == pytest.approx(2400.0)
    live.account["balance"] = 49000.0
    assert g.dd_room() == pytest.approx(1000.0)
    assert g.budget(NOW)[0] == pytest.approx(1000.0)               # daily limit 1000 and drawdown 1000: both 1000


async def test_floor_with_an_unreadable_balance_refuses_to_open(tmp_path):
    g, live, clock, gw = build2(tmp_path, mll_floor=48000.0, dd_buffer=0.0)
    assert g.dd_room() is None and g.budget(NOW) == (None, None)
    p = await prev(g)
    assert not p.ok and "drawdown_known" in failed(p)


async def test_no_room_left_above_the_floor_refuses(tmp_path):
    g, live, clock, gw = build2(tmp_path, mll_floor=48000.0, dd_buffer=0.0)
    live.account["balance"] = 47990.0
    assert "budget_remaining" in failed(await prev(g))


async def test_typed_buffer_still_works_without_a_floor(tmp_path):
    g, live, clock, gw = build2(tmp_path)
    assert g.dd_room() == 2000.0
