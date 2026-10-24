"""Manual BUY / SELL (Round 11): the trader picks the contracts and the stop; the risk is derived and EVERY check still runs.
Same preview, same ticket, same place(). MCP gets no qty."""
import inspect
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from projectx_desk import guard as px_guard
from test_px_app_actions import make_app, post
from test_px_guard_preview import CID, failed, make, prev


async def test_manual_qty_stages_exactly_that_many_with_the_derived_risk(tmp_path):
    g, live, clock = make(tmp_path, max_qty_order=2, max_qty_symbol=3, max_qty_total=3)
    p = await prev(g, risk_dollars=None, qty=2)                       # ask 5000.25, stop 4994: 25 ticks x $1.25 = $31.25 a contract
    assert p.ok, p.reasons
    assert p.ticket.payload["size"] == 2 and p.sizing["contracts"] == 2 and p.sizing["total_risk_dollars"] == 62.5
    assert p.ticket.readback["sized_by"] == "MANUAL" and p.ticket.readback["size"] == 2 and p.ticket.readback["risk_usd"] == 62.5
    assert ("manual_qty", True, "") in p.verdict and ("manual_qty_cap", True, "") in p.verdict
    events = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e for e in events if e["event"] == "manual"][0]["size"] == 2
    one = await prev(g, risk_dollars=None, qty=1)
    assert one.ok and one.ticket.payload["size"] == 1 and one.ticket.readback["risk_usd"] == 31.25
    sized = await prev(g, risk_dollars=50.0)
    assert sized.ok and sized.ticket.readback["sized_by"] == "RISK"


async def test_manual_qty_is_refused_for_exactly_what_the_sizing_would_refuse(tmp_path):
    g, *_ = make(tmp_path)                                             # PX_MAX_QTY_ORDER default 1
    p = await prev(g, risk_dollars=None, qty=2)
    assert not p.ok and "manual_qty_cap" in failed(p)
    g, *_ = make(tmp_path, max_qty_order=3, max_qty_symbol=3, max_qty_total=3, daily_loss_limit=200.0)   # 25% of $200 room caps one trade at $50
    p = await prev(g, risk_dollars=None, qty=2)                       # two contracts want $62.50
    assert not p.ok and "manual_qty" in failed(p)
    assert any("asked 2" in d and "allow 1" in d for n, ok, d in p.verdict if n == "manual_qty")
    p = await prev(g, risk_dollars=None, qty=1)
    assert p.ok and p.ticket.payload["size"] == 1
    g, *_ = make(tmp_path, max_qty_order=3)
    halted = await prev(g, risk_dollars=None, qty=1, stop_price=5010.0)   # stop on the wrong side of a long: the usual checks refuse first
    assert not halted.ok and "stop_side" in failed(halted) and "manual_qty" in failed(halted)


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True, False])
async def test_manual_qty_must_be_a_whole_positive_number(tmp_path, bad):
    g, *_ = make(tmp_path, max_qty_order=3)
    p = await prev(g, risk_dollars=None, qty=bad)
    assert not p.ok and "inputs" in failed(p)


async def test_without_qty_a_missing_risk_is_still_refused(tmp_path):
    g, *_ = make(tmp_path)
    p = await prev(g, risk_dollars=None)
    assert not p.ok and "inputs" in failed(p)


def test_manual_qty_goes_through_the_route_and_mcp_never_offers_it(tmp_path):
    app, g, live, gw = make_app(tmp_path, max_qty_order=2, max_qty_symbol=3, max_qty_total=3)
    with TestClient(app) as c:
        app.state.live = g.live
        tok = c.get("/api/csrf").json()["token"]
        r = post(c, "/api/preview", tok, {"contract": CID, "side": "short", "stop_ticks": 20, "qty": 2, "source": "manual"})
        j = r.json()
        assert j["ok"] and j["ticket"]["readback"]["size"] == 2 and j["ticket"]["readback"]["sized_by"] == "MANUAL"
        assert j["ticket"]["readback"]["side"] == "SELL (short)" and j["ticket"]["readback"]["stop_ticks"] == 20
        assert post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_ticks": 20, "qty": "2"}).status_code == 400
        assert post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_ticks": 20, "qty": True}).status_code == 400
        over = post(c, "/api/preview", tok, {"contract": CID, "side": "long", "stop_ticks": 20, "qty": 3}).json()
        assert not over["ok"] and any(v["check"] == "manual_qty_cap" and not v["ok"] for v in over["verdict"])
    src = pathlib.Path(px_guard.__file__).with_name("mcp.py").read_text(encoding="utf-8")
    assert "qty=" not in src and "qty:" not in src                     # the AI surface sizes by risk only; manual size is the human's
    assert "qty" in inspect.signature(g.preview).parameters


async def test_manual_ticket_is_rechecked_with_its_qty_at_the_commit_point(tmp_path):
    from test_px_guard_place import build
    g, live, clock, gw = build(tmp_path, max_qty_order=2, max_qty_symbol=3, max_qty_total=3)
    p = await g.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=None, qty=2)
    assert p.ok and p.ticket.qty == 2
    r = await g.place(p.ticket.ticket_id, origin="ui", confirm_hash=p.ticket.hash8)
    assert r.ok, r.reasons
    assert gw.writes[0][1]["size"] == 2
    g2, live2, clock2, gw2 = build(tmp_path, max_qty_order=2, max_qty_symbol=3, max_qty_total=3)
    p2 = await g2.preview(contract_id=CID, side="long", stop_price=4994.0, risk_dollars=None, qty=2)
    assert p2.ok
    live2.seed_trades([{"id": 9, "accountId": 7, "contractId": CID, "creationTimestamp": "2026-10-14T14:30:00Z", "profitAndLoss": -950.0, "fees": 0.0,
                        "orderId": 77}])                                            # the room collapsed under the staged ticket
    r2 = await g2.place(p2.ticket.ticket_id, origin="ui", confirm_hash=p2.ticket.hash8)
    assert not r2.ok and any("manual_qty" in x or "budget" in x for x in r2.reasons) and not gw2.writes
