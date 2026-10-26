"""Regressions for the adversarial review of 2026-10-26: bias inputs come from CLOSED bars chosen by the clock, the board never grades at a stale
close, and correlated contracts are not independent trades in the backtest verdict."""
import asyncio
import time
import types
from datetime import datetime, timezone

import pytest

from projectx_desk import biasbt as B
from projectx_desk import digest
from projectx_desk import sizing

BAR = 300


def raw(ts, o, h, l, c):
    return {"t": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "o": o, "h": h, "l": l, "c": c, "v": 10}


class Client:
    def __init__(self, rows):
        self.rows = rows

    async def bars(self, params):
        return self.rows                                               # newest first, like the API


def make_app(rows):
    return types.SimpleNamespace(state=types.SimpleNamespace(client=Client(rows)))


def rows_ending(last_closed_start, n=120, forming=None):
    base = [raw(last_closed_start - i * BAR, 100 + (i % 3) * 0.1, 100.6 + (i % 3) * 0.1, 99.4, 100.2) for i in range(n)]      # newest first
    return ([forming] if forming else []) + base


def test_bias_inputs_come_from_closed_bars_and_the_forming_bar_cannot_leak_in():
    now = time.time()
    forming_start = int(now // BAR) * BAR
    last_closed = forming_start - BAR
    big = raw(forming_start, 100, 130, 70, 120)                       # a violent forming bar
    out = asyncio.run(digest.indicators(make_app(rows_ending(last_closed, forming=big)), "CON.F.US.MES.Z26"))
    assert out["closed_ts"] == last_closed * 1000
    quiet = rows_ending(last_closed)
    hi, lo, cl = ([r[k] for r in reversed(quiet)] for k in ("h", "l", "c"))
    assert out["closed"]["atr"] == pytest.approx(sizing.atr(hi, lo, cl, 14), abs=0.01)             # exactly the closed-only ATR
    assert out["atr"] > out["closed"]["atr"] * 1.5                                                  # the live ATR did see the forming bar; the logged one did not


def test_when_the_new_bar_has_no_print_yet_the_closed_bar_is_still_the_latest_closed_one_not_the_one_before():
    now = time.time()
    last_closed = int(now // BAR) * BAR - BAR
    out = asyncio.run(digest.indicators(make_app(rows_ending(last_closed)), "CON.F.US.MES.Z26"))     # no forming row at all
    assert out["closed_ts"] == last_closed * 1000                      # the old code took index -2 and would have filed the bar BEFORE this one


def test_the_board_does_not_grade_at_a_days_old_close():
    now_ms = time.time() * 1000
    fresh = {"closed_ts": now_ms - 400_000, "closed_close": 7700.0}
    assert digest.board_price(None, fresh) == 7700.0
    assert digest.board_price(None, {"closed_ts": now_ms - 3 * 86_400_000, "closed_close": 7700.0}) is None      # a weekend: NA, not Friday's price
    assert digest.board_price({"lastPrice": 7710.0, "_rx": time.monotonic()}, {"closed_ts": 1, "closed_close": 1.0}) == 7710.0      # a fresh live quote still wins


def test_higher_timeframe_reads_expire_when_the_scan_stops():
    cid = "CON.F.US.MES.Z26"
    bars = [{"close": 100 + i * 0.1, "high": 101 + i * 0.1, "low": 99 + i * 0.1} for i in range(80)]
    st = types.SimpleNamespace(respect_bars={(cid, "15m"): {"at": time.monotonic(), "bars": bars}, (cid, "1h"): {"at": time.monotonic() - 7200, "bars": bars}})
    out = digest.htf_stacks(types.SimpleNamespace(state=st), cid)
    assert out["15m"] != "NA" and out["1h"] == "NA"


def test_contracts_leaning_at_the_same_minute_count_as_one_observation():
    def trade(c, t, r=0.5):
        return {"contract": c, "t": t, "grade": "B", "r": r, "r_opp": -r, "block": (c, t // (6 * 300_000)), "row": {}}
    tr = [trade(c, 1_000_000 + i * 300_000) for i in range(60) for c in ("MES", "MNQ", "MGC")]
    sel = [t for t in tr if t["grade"] == "B"]
    v = B.verdict(tr, 0.01, [{"mean_r": 0.5}] * 3, ("B",), 6)
    assert len(sel) == 180 and v.startswith("INSUFFICIENT") and "about 10 independent" in v        # 60 distinct bars / 6, not 180 / 6 = 30
    mc = B.monte_carlo(tr, ("B",), 0.1, paths=200, length=40, block=6, seed=1)
    assert mc is not None and mc["paths"] == 200
