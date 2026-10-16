"""Stream mode must not leak the size-plan dollar fields (drawdown_left, daily_left, heat_usd, heat_pct)."""
import re

from fastapi.testclient import TestClient

from test_px_sizing_display import prime, rig

CID = "CON.F.US.MES.Z26"
PLAN = ("drawdown_left", "daily_left", "heat_usd", "heat_pct")


def _setup(tmp_path):
    app, g, live = rig(tmp_path, atr=0.8, allowed_roots=("MNQ", "NQ", "MES"), max_qty_order=1)
    return app, g, live


def test_stream_redacts_size_plan_dollars(tmp_path):
    app, g, live = _setup(tmp_path)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        q = {"contract": CID, "risk_pct": 0.05}
        plain = c.get("/ai/state", params=q).json()["size"]
        vals = {k: plain.get(k) for k in PLAN}
        assert all(isinstance(v, (int, float)) for v in vals.values()), vals
        dtxt = c.get("/ai/digest", params=q).text
        for v in (plain["drawdown_left"], plain["daily_left"], plain["heat_usd"]):
            assert re.search(rf"(?<![\d.]){re.escape(f'{v:.2f}'.rstrip('0').rstrip('.'))}(?![\d])", dtxt), v
        assert "heat_after_usd=NA" not in dtxt
        red = c.get("/ai/state", params=q | {"stream": 1}).json()["size"]
        assert all(red.get(k) is None for k in PLAN), red
        stxt = c.get("/ai/digest", params=q | {"stream": 1}).text
        line = next(l for l in stxt.splitlines() if l.startswith("SIZE_IF_STOPPED:"))
        assert line == ("SIZE_IF_STOPPED: drawdown_room_left=NA daily_room_left=NA heat_after_usd=NA heat_after_pct_of_room=NA")
        raw = c.get("/ai/state", params=q | {"stream": 1}).text
        for k in PLAN:
            assert f'"{k}":null' in raw.replace(" ", "")
        for v in (plain["drawdown_left"], plain["daily_left"]):  # large, distinctive: substring match is meaningful
            assert str(v) not in raw and str(v) not in stxt
        assert "STREAM=ON" in stxt


def test_contracts_by_risk_is_redacted_in_stream_mode():                                   # audit finding: with visible stop ticks it bounds the loss room
    from projectx_desk import digest as px_digest
    s = {"risk": {"daily_limit": 500.0, "daily_room": 400.0}, "size": {"contracts_by_risk": 7, "contracts": 1, "stop_ticks": 7}, "kpi": {}}
    assert px_digest.redact_stream(s)["size"]["contracts_by_risk"] is None
