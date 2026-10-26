import time
from types import SimpleNamespace

from projectx_desk import digest as D
from projectx_desk.flow import FlowStore

IND = {"closed_close": 4028.9, "vwap": 4016.85, "ema": {"21": 4031.32, "state": "BULL"}, "atr": 3.37, "adx": 30, "atr_pctile": 50, "rsi": 55}


def test_fresh_quote_wins():
    assert D.board_price({"lastPrice": 4030.1, "_rx": time.monotonic()}, IND) == 4030.1


def test_no_quote_or_a_stale_one_falls_back_to_the_closed_bar():
    assert D.board_price(None, IND) == 4028.9
    assert D.board_price({"lastPrice": 4030.1, "_rx": time.monotonic() - 600}, IND) == 4028.9
    assert D.board_price({"lastPrice": None, "_rx": time.monotonic()}, IND) == 4028.9


def test_nothing_to_price_with_is_none_not_zero():
    assert D.board_price(None, {}) is None and D.board_price(None, None) is None


async def test_a_tracked_root_with_no_quote_still_gets_vwap_and_entry(monkeypatch):
    async def fake_ind(app, cid):
        return IND
    monkeypatch.setattr(D, "indicators", fake_ind)
    monkeypatch.setattr(D, "htf_stacks", lambda app, cid: {"15m": "BULL", "1h": "BULL"})
    app = SimpleNamespace(state=SimpleNamespace(
        live=SimpleNamespace(quotes={}, flow=FlowStore(path=None)), tracked={"MGC": "CON.F.US.MGC.Z26"}))
    row = (await D.bias_board(app))["rows"][0]
    assert row["root"] == "MGC" and "ENTRY_NA" not in row["flags"] and row["entry"] is not None
    assert row["reads"] >= 4 and row["direction"] == "LONG"
