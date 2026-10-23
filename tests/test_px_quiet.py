"""Market-closed quiet: background API traffic pauses Fri 16:05 CT to Sun 16:55 CT, but only while the desk knows it is flat."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

from projectx_desk import app as px_app
from projectx_desk import hub as px_hub
from projectx_desk.tradeday import CT, market_quiet


def ct(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CT)


def test_quiet_window_boundaries():
    # 2026-10-23 is a Friday
    assert not market_quiet(ct(2026, 10, 23, 16, 4))
    assert market_quiet(ct(2026, 10, 23, 16, 5))
    assert market_quiet(ct(2026, 10, 24, 3))                 # Saturday, any hour
    assert market_quiet(ct(2026, 10, 25, 16, 54))            # Sunday before the reopen
    assert not market_quiet(ct(2026, 10, 25, 16, 55))
    assert not market_quiet(ct(2026, 10, 26, 3))             # Monday
    assert not market_quiet(ct(2026, 10, 22, 20))            # Thursday evening


def _app(positions=(), orders=(), seeded=True):
    live = SimpleNamespace(positions={i: 1 for i in positions}, orders={i: 1 for i in orders}, positions_seeded=seeded)
    return SimpleNamespace(state=SimpleNamespace(live=live))


def test_quiet_needs_a_provably_flat_desk(monkeypatch):
    monkeypatch.delenv("PX_WEEKEND_QUIET", raising=False)
    monkeypatch.setattr(px_app.px_tradeday, "market_quiet", lambda now=None: True)
    assert px_app._quiet(_app())
    assert not px_app._quiet(_app(positions=[1]))            # exposure: keep watching
    assert not px_app._quiet(_app(orders=[7]))
    assert not px_app._quiet(_app(seeded=False))             # unknown is not flat
    monkeypatch.setenv("PX_WEEKEND_QUIET", "0")
    assert not px_app._quiet(_app())                         # explicit off
    monkeypatch.setenv("PX_WEEKEND_QUIET", "garbled")
    assert px_app._quiet(_app())                             # garbled falls back to the default (on)
    monkeypatch.setattr(px_app.px_tradeday, "market_quiet", lambda now=None: False)
    assert not px_app._quiet(_app())


def test_a_quiet_hub_does_not_reconnect_and_resumes_when_the_window_ends(monkeypatch):
    monkeypatch.setattr(px_hub, "QUIET_POLL_S", 0.01)
    calls = []

    class Client:
        async def token(self, fresh=False):
            calls.append(1)
            raise RuntimeError("stop here")             # the connect attempt is all this test needs to see

    async def go():
        h = px_hub.SignalRHub(Client(), "market", url="ws://127.0.0.1:1/x")
        state = {"quiet": True}
        h.quiet = lambda: state["quiet"]
        h.start()
        await asyncio.sleep(0.15)
        assert calls == []                              # no login, no websocket while quiet
        state["quiet"] = False
        await asyncio.sleep(0.15)
        assert calls                                    # the window ended: it tries again
        await h.stop()

    asyncio.run(go())


def test_a_broken_quiet_predicate_never_blocks_reconnecting():
    h = px_hub.SignalRHub(object(), "market", url="ws://127.0.0.1:1/x")
    h.quiet = lambda: 1 / 0
    assert h._quiet_now() is False
