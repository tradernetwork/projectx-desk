"""Trade journal: allowlisted fills, round-trip pairing (adds, partials, flips, open), no-lookahead BIAS join, the MIN_N rule, the ledger, and the pin."""
import ast
import json
import pathlib
import re
from datetime import datetime, timezone

import pytest

from projectx_desk import journal as j

ROOT = pathlib.Path(__file__).resolve().parent.parent
MES = "CON.F.US.MES.Z26"


def raw(i, side, price, size=1, pnl=None, fees=0.37, ts=None, cid=MES, order=None):
    return {"id": i, "accountId": 99, "contractId": cid, "creationTimestamp": ts or f"2026-10-29T14:{i:02d}:00+00:00", "price": price,
            "profitAndLoss": pnl, "fees": fees, "side": side, "size": size, "voided": False, "orderId": order or i}


def trips(*rows):
    return j.round_trips(j.fill_rows(list(rows)))


# ---- fills ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {}, None, [], {**raw(1, 0, 7700), "voided": True}, {**raw(1, 2, 7700)}, {**raw(1, True, 7700)}, {**raw(1, 0, -1)}, {**raw(1, 0, float("nan"))},
    {**raw(1, 0, 7700, size=0)}, {**raw(1, 0, 7700, size=1.5)}, {**raw(1, 0, 7700, size=5000)}, {**raw(True, 0, 7700)},
    {**raw(1, 0, 7700, cid="evil; DROP")}, {**raw(1, 0, 7700), "creationTimestamp": "garbage"},
])
def test_bad_fills_are_dropped(bad):
    assert j.fill_row(bad) is None


def test_fill_row_keeps_only_the_allowlist():
    r = j.fill_row({**raw(7, 1, 7716.25, pnl=12.5), "accountId": 28659217, "customTag": "secret"})
    assert set(r) == {"id", "ts", "contract", "side", "price", "size", "pnl", "fees", "order"} and r["side"] == "SELL"
    assert "28659217" not in json.dumps(r) and "secret" not in json.dumps(r)


# ---- round trips ---------------------------------------------------------------------------------------------------------

def test_long_and_short_round_trips_net_of_fees():
    t = trips(raw(1, 0, 7700), raw(2, 1, 7710, pnl=50.0), raw(3, 1, 7720), raw(4, 0, 7712, pnl=40.0))
    assert [(x["side"], x["avg_entry"], x["avg_exit"], x["net"], x["hold_s"]) for x in t] == [("LONG", 7700, 7710, 49.26, 60), ("SHORT", 7720, 7712, 39.26, 60)]
    assert all(not x["open"] for x in t)


def test_scale_in_and_partial_exits_are_one_trip_with_prorated_fees():
    t = trips(raw(1, 0, 7700), raw(2, 0, 7702), raw(3, 1, 7710, size=1, pnl=40.0), raw(4, 1, 7712, size=1, pnl=50.0))
    assert len(t) == 1 and t[0]["qty"] == 2 and t[0]["n_entries"] == 2 and t[0]["n_exits"] == 2
    assert t[0]["avg_entry"] == 7701 and t[0]["avg_exit"] == 7711 and t[0]["pnl"] == 90.0 and t[0]["net"] == round(90 - 4 * 0.37, 2)


def test_a_fill_that_crosses_flat_is_split_between_two_trips():
    t = trips(raw(1, 0, 7700), raw(2, 1, 7710, size=3, pnl=10.0, fees=0.9))      # sells 3 against 1 long: closes it, opens a 2 short
    assert len(t) == 2 and t[0]["side"] == "LONG" and not t[0]["open"] and t[1]["side"] == "SHORT" and t[1]["open"] and t[1]["qty"] == 2
    assert t[0]["fees"] == round(0.37 + 0.9 / 3, 4) and t[0]["pnl"] == round(10.0 / 3, 2)


def test_an_unclosed_position_is_open_and_has_no_result():
    t = trips(raw(1, 0, 7700))
    assert t[0]["open"] and t[0]["net"] is None and t[0]["hold_s"] is None and t[0]["avg_exit"] is None
    assert j.summarize(t)["n"] == 0


def test_contracts_are_paired_separately():
    t = trips(raw(1, 0, 7700), raw(2, 0, 29000, cid="CON.F.US.MNQ.Z26"), raw(3, 1, 7701, pnl=5.0), raw(4, 1, 29010, pnl=20.0, cid="CON.F.US.MNQ.Z26"))
    assert sorted(x["contract"].split(".")[3] for x in t) == ["MES", "MNQ"] and all(not x["open"] for x in t)


# ---- context -------------------------------------------------------------------------------------------------------------

def test_bias_join_uses_the_last_read_at_or_before_entry_never_after_and_never_stale():
    t = trips(raw(1, 0, 7700, ts="2026-10-29T14:10:00+00:00"), raw(2, 1, 7710, pnl=5.0, ts="2026-10-29T14:20:00+00:00"))
    e = int(datetime(2026, 10, 29, 14, 10, tzinfo=timezone.utc).timestamp() * 1000)
    bias = {MES: [{"ts": e - 60000, "grade": "B", "direction": "LONG", "score": 2.0, "adx": 25.0, "flags": []},
                  {"ts": e + 1, "grade": "A", "direction": "SHORT", "score": 3.0, "adx": 30.0, "flags": []}]}
    assert j.attach_context(t, bias)[0]["ctx"] == {"grade": "B", "direction": "LONG", "flags": [], "aligned": True}
    bias[MES][0]["ts"] = e - 11 * 60000
    assert j.attach_context(t, bias)[0]["ctx"] is None
    bias[MES][0].update(ts=e - 1000, direction="SHORT")
    assert j.attach_context(t, bias)[0]["ctx"]["aligned"] is False
    bias[MES][0].update(direction="NEUTRAL")
    assert j.attach_context(t, bias)[0]["ctx"]["aligned"] is None


# ---- summary and memory --------------------------------------------------------------------------------------------------

def many(n):
    rows = []
    for k in range(n):
        base = k * 4
        win = k % 2 == 0
        rows += [raw(base + 1, 0, 7700, ts=f"2026-10-{1 + k // 20:02d}T{14 + k % 20 // 4:02d}:{k % 4 * 10:02d}:00+00:00"),
                 raw(base + 2, 1, 7705 if win else 7697, pnl=25.0 if win else -15.0, ts=f"2026-10-{1 + k // 20:02d}T{14 + k % 20 // 4:02d}:{k % 4 * 10 + 5:02d}:00+00:00")]
    return rows


def test_rates_are_withheld_under_min_n_and_shown_at_it():
    small, big = j.summarize(trips(*many(10))), j.summarize(trips(*many(j.MIN_N)))
    assert small["win_rate"] is None and small["payoff"] is None and small["n"] == 10
    assert big["n"] == j.MIN_N and big["win_rate"] == 50.0 and big["payoff"] == round(24.26 / 15.74, 2)


def test_habit_counts_revenge_scale_in_and_late_entries():
    rows = [raw(1, 0, 7700, ts="2026-10-29T14:00:00+00:00"), raw(2, 1, 7690, pnl=-50.0, ts="2026-10-29T14:05:00+00:00"),    # loss
            raw(3, 0, 7690, ts="2026-10-29T14:08:00+00:00"), raw(4, 0, 7689, ts="2026-10-29T14:09:00+00:00"),                # revenge, scale-in
            raw(5, 1, 7700, size=2, pnl=20.0, ts="2026-10-29T14:30:00+00:00"),
            raw(6, 0, 7700, ts="2026-10-29T20:00:00+00:00"), raw(7, 1, 7701, pnl=5.0, ts="2026-10-29T20:01:00+00:00")]        # 15:00 CT
    s = j.summarize(trips(*rows))
    assert s["revenge"]["n"] == 1 and s["scaled_in"]["n"] == 1 and s["after_1455"]["n"] == 1 and s["n"] == 3


def test_memory_is_plain_facts_with_sample_sizes_and_no_account_data(tmp_path):
    led = j.Ledger(tmp_path / "trades.jsonl")
    led.add(j.fill_rows(many(6)))
    b = tmp_path / "bias.jsonl"
    b.write_text(json.dumps({"ts": 0, "contract": MES, "grade": "B", "direction": "LONG", "score": 2.0}) + "\n")
    out = tmp_path / "mem"
    import os
    os.environ["PX_MEMORY_DIR"] = str(out)
    try:
        trips_ = j.write_memory(tmp_path, ledger_path=tmp_path / "trades.jsonl", bias_path=b, now=datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc))
    finally:
        del os.environ["PX_MEMORY_DIR"]
    text = (out / "trades.md").read_text()
    assert len(trips_) == 6 and "n=6" in text and "withheld: n<30" in text and "| MES |" in text
    assert not (out / "trades.md.tmp").exists() and "99" not in re.sub(r"\d{2,}[-:.]\d+", "", text).replace("7699", "")


# ---- ledger --------------------------------------------------------------------------------------------------------------

def test_ledger_is_append_only_deduped_and_survives_reload(tmp_path):
    p = tmp_path / "t.jsonl"
    led = j.Ledger(p)
    rows = j.fill_rows([raw(1, 0, 7700), raw(2, 1, 7701, pnl=4.0)])
    assert len(led.add(rows)) == 2 and led.add(rows) == []
    before = p.read_text()
    assert j.Ledger(p).ids == {1, 2} and len(j.Ledger(p).add(rows)) == 0 and p.read_text() == before
    led.add(j.fill_rows([raw(3, 0, 7702)]))
    assert p.read_text().startswith(before) and len(p.read_text().splitlines()) == 3
    p.write_text(before + "not json\n")
    assert j.Ledger(p).ids == {1, 2}


# ---- pin: the journal cannot reach the order side ------------------------------------------------------------------------

def test_journal_module_cannot_reach_the_order_side():
    src = (ROOT / "projectx_desk" / "journal.py").read_text()
    tree = ast.parse(src)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if re.search(r"guard|client|hub|mcp|app", m)}, imported
    assert not re.search(r"/api/Order|place_order|\.write\(", src)
