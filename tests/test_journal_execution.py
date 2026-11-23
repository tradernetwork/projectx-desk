"""Journal execution facts: slippage vs a recorded arrival quote, size drift vs the trader's own median, loss streaks."""
import json
from datetime import datetime, timezone

from projectx_desk import journal as j
from projectx_desk import scalp

MNQ = "CON.F.US.MNQ.Z26"
T0 = int(datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)


def mk(i, net=1.0, qty=1, side="LONG", entry=100.0, n_entries=1, contract=MNQ, open_=False):
    return {"contract": contract, "side": side, "entry_ts": T0 + i * 600_000, "exit_ts": T0 + i * 600_000 + 60_000, "qty": qty,
            "avg_entry": entry, "avg_exit": entry, "n_entries": n_entries, "open": open_, "net": None if open_ else net,
            "pnl": net, "fees": 0.0, "hold_s": 60}


def arr(ts, price, side="BUY", contract=MNQ):
    return {contract: [{"ts": ts, "side": side, "price": price}]}


def test_slippage_long_paying_up_is_positive_adverse():
    t = mk(0, entry=100.5)
    j.attach_slippage([t], arr(t["entry_ts"] - 1000, 100.0))
    assert t["slip_ticks"] == 2.0                      # 0.5 points on a 0.25 tick


def test_slippage_short_selling_lower_is_adverse_and_better_fill_is_negative():
    t = mk(0, side="SHORT", entry=99.5)
    j.attach_slippage([t], arr(t["entry_ts"] - 1000, 100.0, "SELL"))
    assert t["slip_ticks"] == 2.0
    u = mk(1, side="SHORT", entry=100.25)
    j.attach_slippage([u], arr(u["entry_ts"] - 1000, 100.0, "SELL"))
    assert u["slip_ticks"] == -1.0


def test_slippage_is_na_without_a_usable_arrival():
    t = mk(0)
    j.attach_slippage([t], {})
    assert t["slip_ticks"] is None                                                    # none recorded
    j.attach_slippage([t], arr(t["entry_ts"] + 1, 100.0))
    assert t["slip_ticks"] is None                                                    # after the entry: lookahead
    j.attach_slippage([t], arr(t["entry_ts"] - j.ARRIVAL_MAX_AGE_MS - 1, 100.0))
    assert t["slip_ticks"] is None                                                    # stale
    j.attach_slippage([t], arr(t["entry_ts"] - 1, 100.0, "SELL"))
    assert t["slip_ticks"] is None                                                    # wrong side
    j.attach_slippage([t], arr(t["entry_ts"] - 1, 100.0, contract="CON.F.US.MES.Z26"))
    assert t["slip_ticks"] is None                                                    # other contract


def test_slippage_na_for_scale_in_unknown_tick_and_never_zero():
    t = mk(0, n_entries=2)
    u = mk(1, contract="CON.F.US.ZZZ.Z26")
    j.attach_slippage([t, u], {**arr(t["entry_ts"] - 1, 100.0), **arr(u["entry_ts"] - 1, 100.0, contract="CON.F.US.ZZZ.Z26")})
    assert t["slip_ticks"] is None and u["slip_ticks"] is None


def test_slippage_average_withheld_under_min_n_but_n_shown():
    ts = [mk(i, entry=100.25) for i in range(3)]
    for t in ts:
        t["slip_ticks"] = 1.0
    s = j.slippage_stats(ts)
    assert s["n"] == 3 and s["avg"] is None and s["adverse"] == 3
    ts = [mk(i) for i in range(j.MIN_N)]
    for t in ts:
        t["slip_ticks"] = 1.0
    assert j.slippage_stats(ts)["avg"] == 1.0
    assert j.slippage_stats([mk(0)])["n"] == 0


def test_read_arrivals_skips_garbage_and_missing_file(tmp_path):
    p = tmp_path / "a.jsonl"
    good = {"ts": 5, "contract": MNQ, "side": "BUY", "price": 100.0}
    lines = [json.dumps(good), "not json", json.dumps({**good, "price": 0}), json.dumps({**good, "side": "X"}),
             json.dumps({**good, "ts": True}), json.dumps({**good, "contract": "bad"}), json.dumps({**good, "price": "100"})]
    p.write_text("\n".join(lines))
    assert j.read_arrivals(p) == {MNQ: [{"ts": 5, "side": "BUY", "price": 100.0}]}
    assert j.read_arrivals(tmp_path / "none") == {}


def test_size_drift_na_under_min_n_then_flags_oversized_without_lookahead():
    ts = [mk(i, qty=1) for i in range(j.MIN_N)]
    d = j.size_drift(ts)
    assert d["n"] == 0 and d["of"] == j.MIN_N and ts[-1]["size_ratio"] is None
    ts.append(mk(j.MIN_N, qty=4))
    ts.append(mk(j.MIN_N + 1, qty=1))
    d = j.size_drift(ts)
    assert d["n"] == 2 and d["oversized"] == 1 and d["max_ratio"] == 4.0
    assert ts[-2]["size_ratio"] == 4.0 and ts[-1]["size_ratio"] == 1.0              # the 4-lot did not move its own baseline


def test_size_drift_ignores_open_trips():
    ts = [mk(i) for i in range(j.MIN_N)] + [mk(99, qty=9, open_=True)]
    assert j.size_drift(ts)["of"] == j.MIN_N


def test_loss_streaks_longest_current_and_scratch_breaks():
    nets = [1, -1, -1, -1, 0, -1, -1, 2, -1]
    s = j.loss_streaks([mk(i, net=n) for i, n in enumerate(nets)])
    assert s == {"n": 9, "longest": 3, "current": 1}
    assert j.loss_streaks([]) == {"n": 0, "longest": 0, "current": 0}
    assert j.loss_streaks([mk(0, open_=True)])["n"] == 0


def test_summary_memory_and_scalp_render_na_and_n():
    ts = [mk(i, net=-1.0) for i in range(4)]
    for t in ts:
        t["ctx"], t["x"], t["hold_s"] = None, None, 60
    s = j.summarize(ts)
    assert s["streak"]["longest"] == 4 and s["slippage"]["n"] == 0 and s["size_drift"]["n"] == 0
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    md = j.render_memory(ts, now)
    assert "Longest run of consecutive losing trips: 4 (n=4" in md
    assert "NA, no arrival quote recorded (n=0)" in md and "NA, needs 30 earlier trips" in md
    sc = scalp.render_scalp(ts, now)
    assert "Longest losing streak: 4 trips in a row (n=4" in sc and "no arrival quote was recorded" in sc


def test_write_memory_with_no_trips_or_arrivals_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("PX_ARRIVALS_FILE", str(tmp_path / "arr.jsonl"))
    monkeypatch.setenv("PX_BIAS_LOG_FILE", str(tmp_path / "bias.jsonl"))
    out = j.write_memory(root=tmp_path, ledger_path=tmp_path / "t.jsonl")
    assert out == [] and (tmp_path / "mem" / "trades.md").exists()
