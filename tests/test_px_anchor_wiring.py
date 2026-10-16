"""How px_anchor is wired into the size box: only on the default chip, only if it stays inside the stop-tick cap and the budget."""
import dataclasses

import px_digest
from px_limits import Limits

REC = {"tickSize": 0.25, "tickValue": 1.25}
CID = "CON.F.US.MES.Z26"
SCALE = {"multiplier": 1.0, "reasons": [], "reduced": False, "status": "ok"}
LEVELS = [{"id": "kc_lo20", "tf": "15m", "price": 7774.25, "verdict": "ABOVE_RANDOM"},    # 10.25 below a 7784.5 entry
          {"id": "pdc", "tf": "5m", "price": 7799.0, "verdict": "ABOVE_RANDOM"}]


def size(stop_k=1.5, chosen=100.0, levels=LEVELS, loss_cap=None, **lim):
    limits = dataclasses.replace(Limits(), **lim) if lim else Limits()
    return px_digest.size_idea(limits, CID, REC, "long", 7784.5, 6.0, 25.0, 0.05, stop_k, 500.0, chosen, SCALE, 50, loss_cap,
                               levels=levels, rooms=(1900.0, 450.0, 0.0, 2000.0))


def test_default_chip_anchors_beyond_the_respected_level_and_shows_target_and_room():
    z = size()
    assert z["stop_mode"] == "ANCHORED" and z["stop_anchor"] == "15m kc_lo20"
    assert z["stop_ticks"] == 44 and z["stop"] == 7773.5          # base 36t; level 7774.25 minus buffer 0.75 => 11.0pt
    assert z["target"] == 7799.0 and z["rr"] == 1.32 and z["breakeven_win_rate"] == 43.1
    assert z["drawdown_left"] == 1900.0 - z["risk_usd"] and z["daily_left"] == 450.0 - z["risk_usd"]
    assert z["contracts"] == 1 and z["risk_usd"] == 55.0


def test_manual_chips_bypass_the_anchor():
    for k in (1.0, 2.0, 1.5001):
        z = size(stop_k=k)
        assert z["stop_mode"] == "MANUAL" and z["stop_anchor"] is None and "target" not in z


def test_anchor_dropped_when_it_would_exceed_the_stop_tick_cap():
    z = size(max_stop_ticks=40)                                   # anchored 44t > 40t cap: plain ATR stop (36t) stands
    assert z["stop_mode"] == "ATR" and z["stop_ticks"] == 36


def test_anchor_never_creates_a_refusal_the_base_stop_avoids():
    z = size(chosen=50.0)                                         # anchored $55/ct > $50 budget, base $45/ct fits
    assert z["stop_mode"] == "ATR" and z["stop_ticks"] == 36 and not z["refused"] and z["contracts"] == 1


def test_no_levels_means_plain_atr_and_no_target():
    z = size(levels=None)
    assert z["stop_mode"] == "ATR" and "target" not in z and z["stop_ticks"] == 36
