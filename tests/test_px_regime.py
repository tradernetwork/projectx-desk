"""ADX is a regime advisory (RANGING/TRENDING/UNKNOWN), not part of the size. Display only: no guard check reads it."""
import pytest
from fastapi.testclient import TestClient

from projectx_desk import digest as px_digest
from test_px_sizing_display import digest, inject_ind, prime, rig

CID = "CON.F.US.MNQ.Z26"


@pytest.mark.parametrize("adx,state", [(17.2, "RANGING"), (19.99, "RANGING"), (20.0, "TRENDING"), (31.0, "TRENDING"), (None, "UNKNOWN")])
def test_regime_state_and_digest_line(tmp_path, monkeypatch, adx, state):
    app, g, live = rig(tmp_path, atr=0.8, scalers=False)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        inject_ind(monkeypatch, adx, 50.0)
        r = c.get("/ai/state", params={"contract": CID, "risk_pct": 0.05}).json()["regime"]
        assert r == {"state": state, "adx": adx, "threshold": 20.0, "tf": "5m"}
        line = next(x for x in digest(c, CID).splitlines() if x.startswith("REGIME:"))
        if state == "RANGING":
            assert line == f"REGIME: RANGING adx_5m={adx:.1f} threshold=20 (trend setups unreliable, sit-out zone)"
        elif state == "TRENDING":
            assert line == f"REGIME: TRENDING adx_5m={adx:.1f} threshold=20"
        else:
            assert line == "REGIME: UNKNOWN"
        assert "IND: atr_5m=" in digest(c, CID)


def test_regime_does_not_change_size_or_mention_adx(tmp_path, monkeypatch):
    app, g, live = rig(tmp_path, atr=0.8, scalers=False)
    with TestClient(app) as c:
        app.state.live = g.live
        prime(app, g, live, atr=0.8)
        out = []
        for adx in (10.0, 40.0, None):
            inject_ind(monkeypatch, adx, 50.0)
            z = c.get("/ai/state", params={"contract": CID, "risk_pct": 0.05}).json()["size"]
            assert "Size: ATR stop only" in z["why"] and z["scale"]["multiplier"] == 1.0
            assert not any("ADX" in w or "scaler" in w.lower() for w in z["why"])
            out.append((z["contracts"], z["stop_ticks"], z["budget_usd"]))
        assert len(set(out)) == 1


def test_regime_view_never_guesses():
    assert px_digest.regime_view(float("nan"))["state"] == "UNKNOWN"
    assert px_digest.regime_view("30")["state"] == "UNKNOWN"
    assert px_digest.regime_view(True)["state"] == "UNKNOWN"
