"""Per-contract trade-print age: a hub that is up and has frames can still have a dead trade subscription for one contract."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from projectx_desk import tapewatch
from projectx_desk.state import LiveState
from projectx_desk.tradeday import CT, market_open


def test_stale_s_env(monkeypatch):
    monkeypatch.delenv("PX_TAPE_STALE_S", raising=False)
    assert tapewatch.stale_s() == 300.0
    monkeypatch.setenv("PX_TAPE_STALE_S", "600")
    assert tapewatch.stale_s() == 600.0
    monkeypatch.setenv("PX_TAPE_STALE_S", "10")
    assert tapewatch.stale_s() == 60.0           # floor
    monkeypatch.setenv("PX_TAPE_STALE_S", "0")
    assert tapewatch.stale_s() == 0.0            # off
    monkeypatch.setenv("PX_TAPE_STALE_S", "garbled")
    assert tapewatch.stale_s() == 300.0
    monkeypatch.setenv("PX_TAPE_STALE_S", "")
    assert tapewatch.stale_s() == 300.0


def test_market_open_excludes_the_daily_break_and_the_weekend():
    assert market_open(datetime(2026, 11, 16, 10, 0, tzinfo=CT))          # Monday 10:00
    assert not market_open(datetime(2026, 11, 16, 16, 30, tzinfo=CT))     # 16:00 to 17:00 CT break
    assert market_open(datetime(2026, 11, 16, 17, 0, tzinfo=CT))
    assert not market_open(datetime(2026, 11, 21, 12, 0, tzinfo=CT))      # Saturday


def test_live_state_records_the_last_print_per_contract():
    s = LiveState()
    assert s.tape_last("C1") is None
    s._on_trade("C1", {"timestamp": 1_700_000_000_000, "volume": 1, "type": 0})
    assert s.tape_last("C1") is not None
    assert s.tape_last("C2") is None


def test_evaluate_flags_only_when_open_and_hub_up():
    t = 10_000.0
    last = {"C1": t - 10, "C2": t - 900}
    tracked = {"MES": "C1", "MGC": "C2"}
    out = tapewatch.evaluate(tracked, last, {}, t, 300.0, hub_up=True, is_open=True)
    assert out["MES"] == {"age_s": 10.0, "stale": False}
    assert out["MGC"] == {"age_s": 900.0, "stale": True}
    assert not tapewatch.evaluate(tracked, last, {}, t, 300.0, hub_up=True, is_open=False)["MGC"]["stale"]
    assert not tapewatch.evaluate(tracked, last, {}, t, 300.0, hub_up=False, is_open=True)["MGC"]["stale"]
    assert not tapewatch.evaluate(tracked, last, {}, t, 0.0, hub_up=True, is_open=True)["MGC"]["stale"]   # off


def test_never_printed_ages_from_the_subscription_start():
    t = 5_000.0
    out = tapewatch.evaluate({"MGC": "C2"}, {}, {"C2": t - 400}, t, 300.0, hub_up=True, is_open=True)
    assert out["MGC"] == {"age_s": None, "stale": True}
    out = tapewatch.evaluate({"MGC": "C2"}, {}, {"C2": t - 100}, t, 300.0, hub_up=True, is_open=True)
    assert out["MGC"]["stale"] is False


class FakeLive:
    def __init__(self):
        self.calls, self._last_print, self._tape_start = [], {}, {"C2": 0.0}

    async def resubscribe_tape(self, cid):
        self.calls.append(cid)


def _step(live, memo, now, **kw):
    args = dict(tracked={"MGC": "C2"}, hub_up=True, is_open=True, quiet=False, thresh=300.0)
    args.update(kw)
    return asyncio.run(tapewatch.step(live, memo, now, **args))


def test_step_resubscribes_once_per_five_minutes(capsys):
    live, memo = FakeLive(), {}
    _step(live, memo, 1000.0)
    assert live.calls == ["C2"]
    assert "tape: stale print age, resubscribed MGC" in capsys.readouterr().out
    _step(live, memo, 1200.0)
    assert live.calls == ["C2"]                       # inside the 300 s cooldown
    _step(live, memo, 1301.0)
    assert live.calls == ["C2", "C2"]


def test_step_stands_down_when_quiet_closed_down_or_off():
    for kw in ({"quiet": True}, {"is_open": False}, {"hub_up": False}, {"thresh": 0.0}):
        live = FakeLive()
        _step(live, {}, 1000.0, **kw)
        assert live.calls == []


def test_resubscribe_tape_replays_the_trade_subscription():
    class Hub:
        def __init__(self):
            self.log = []

        def on(self, *a):
            pass

        def listen(self, *a):
            pass

        async def unsubscribe(self, *a):
            self.log.append(("u",) + a)

        async def subscribe(self, *a, **k):
            self.log.append(("s",) + a)

    hub = Hub()
    s = LiveState(hub)
    s._tape.append("C2")
    asyncio.run(s.resubscribe_tape("C2"))
    assert hub.log == [("u", "SubscribeContractTrades", "C2"), ("s", "SubscribeContractTrades", "C2")]
    asyncio.run(s.resubscribe_tape("NOPE"))               # not a recorded tape: nothing sent
    assert len(hub.log) == 2
