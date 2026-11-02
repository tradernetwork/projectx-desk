"""Scalp analytics: excursions, no-lookahead entry features, slices, the MIN_N and SMALL rules, the markdown, the chart PNG, and the report script."""
import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from projectx_desk import journal as j
from projectx_desk import scalp, tripchart

ROOT = pathlib.Path(__file__).resolve().parent.parent
MNQ = "CON.F.US.MNQ.Z26"
BASE = int(datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)     # 09:00 CT
MIN = 60_000


def bars(n, start=BASE, unit=1, price=28500.0, drift=0.25, rng=2.0):
    """Deterministic synthetic bars, ascending, each `unit` minutes."""
    out, p = [], price
    for i in range(n):
        o, c = p, p + drift * (1 if i % 3 else -1)
        out.append({"timestamp": start + i * unit * MIN, "open": o, "high": max(o, c) + rng / 2, "low": min(o, c) - rng / 2, "close": c, "volume": 100 + i % 7})
        p = c
    return out


def trip(entry_min=60, hold_min=3, side="LONG", entry=28510.0, exit_=28514.0, qty=1, cid=MNQ, net_pnl=None):
    """A closed journal trip built through the real pairing code."""
    d = 1 if side == "LONG" else -1
    pnl = net_pnl if net_pnl is not None else (exit_ - entry) * d * 2.0 * qty
    ts = lambda m: datetime.fromtimestamp((BASE + m * MIN) / 1000, timezone.utc).isoformat()       # noqa: E731
    a = {"id": entry_min * 10, "contractId": cid, "creationTimestamp": ts(entry_min), "price": entry, "profitAndLoss": None, "fees": 0.37, "side": 0 if side == "LONG" else 1, "size": qty, "voided": False}
    b = {"id": entry_min * 10 + 1, "contractId": cid, "creationTimestamp": ts(entry_min + hold_min), "price": exit_, "profitAndLoss": pnl, "fees": 0.37, "side": 1 if side == "LONG" else 0, "size": qty, "voided": False}
    return j.round_trips(j.fill_rows([a, b]))[0]


# ---- excursions -----------------------------------------------------------------------------------------------------------

def test_excursion_long_uses_high_for_favourable_and_low_for_adverse():
    t = trip(entry_min=10, hold_min=4, entry=100.0, exit_=101.0)
    bs = [{"timestamp": BASE + (10 + i) * MIN, "open": 100, "high": h, "low": lo, "close": 100.5, "volume": 1} for i, (h, lo) in enumerate([(103, 99.5), (101, 97), (102, 99), (101, 100), (110, 90)])]
    e = scalp.excursion(t, bs)
    assert e["mfe"] == 3.0 and e["mae"] == 3.0            # the bar starting AT the exit (minute 14, 110/90) holds only post-exit time and is out
    assert e["real"] == 1.0 and e["bars"] == 4 and e["coarse"] is False


def test_excursion_is_never_smaller_than_what_was_realized():
    t = trip(entry_min=10, hold_min=1, entry=100.0, exit_=104.0)
    flat = [{"timestamp": BASE + 10 * MIN, "open": 100, "high": 100.0, "low": 100.0, "close": 100, "volume": 1}]
    e = scalp.excursion(t, flat)
    assert e["mfe"] == 4.0 and e["mae"] == 0.0 and e["coarse"] is True


def test_excursion_short_flips_the_sides():
    t = trip(entry_min=10, hold_min=2, side="SHORT", entry=100.0, exit_=99.0)
    bs = [{"timestamp": BASE + (10 + i) * MIN, "open": 100, "high": 102, "low": 97, "close": 99, "volume": 1} for i in range(3)]
    e = scalp.excursion(t, bs)
    assert e["mfe"] == 3.0 and e["mae"] == 2.0 and e["real"] == 1.0


def test_excursion_none_without_bars_or_when_open():
    assert scalp.excursion(trip(), []) is None
    t = trip()
    t["open"] = True
    assert scalp.excursion(t, bars(5)) is None


# ---- entry features: no lookahead ----------------------------------------------------------------------------------------

def test_features_ignore_bars_that_had_not_closed_at_entry():
    t = trip(entry_min=100, hold_min=3)
    one, five = bars(140), bars(90, start=BASE - 300 * MIN, unit=5)
    base = scalp.entry_features(t, one, five)
    poisoned = [dict(b) for b in one]
    for b in poisoned:
        if b["timestamp"] + MIN > t["entry_ts"]:               # every bar that closes after the entry
            b.update(open=1.0, high=1e6, low=-1e6, close=1e6)
    p5 = [dict(b) for b in five]
    for b in p5:
        if b["timestamp"] + 5 * MIN > t["entry_ts"]:
            b.update(open=1.0, high=1e6, low=-1e6, close=1e6)
    assert scalp.entry_features(t, poisoned, p5) == base
    assert base["atr1"] is not None and base["ext5"] is not None


def test_features_none_when_history_is_short():
    t = trip(entry_min=10)
    f = scalp.entry_features(t, bars(15), bars(5, unit=5))
    assert f["ext1"] is None and f["ext5"] is None and f["trend5"] is None


def test_extension_sign_follows_the_trade_direction():
    one, five = bars(140), bars(90, start=BASE - 300 * MIN, unit=5)
    long_t, short_t = trip(entry_min=100, side="LONG", entry=28600.0, exit_=28601.0), trip(entry_min=100, side="SHORT", entry=28600.0, exit_=28599.0)
    a, b = scalp.entry_features(long_t, one, five), scalp.entry_features(short_t, one, five)
    assert a["ext5"] == pytest.approx(-b["ext5"], abs=0.02) and a["ext5"] > 1.0     # far above the EMA: long is extended, short is a fade


# ---- slices and the rules -------------------------------------------------------------------------------------------------

def test_hold_buckets():
    assert [scalp.hold_bucket({"hold_s": s}) for s in (10, 60, 179, 180, 599, 600, 7200)] == ["under 1m", "1-3m", "1-3m", "3-10m", "3-10m", "10m+", "10m+"]


def test_after_loss_label_uses_same_contract_only():
    a = trip(entry_min=10, hold_min=2, entry=100, exit_=98)            # loss, exits at minute 12
    b = trip(entry_min=15, hold_min=1, entry=100, exit_=101)          # 3 min after the loss -> revenge window
    c = trip(entry_min=15, hold_min=1, entry=100, exit_=101, cid="CON.F.US.MES.Z26")
    lab = scalp._after_loss([a, b, c])
    assert lab[id(b)] == "<10m after a loss" and lab[id(c)] == "no loss before" and lab[id(a)] == "no loss before"


def test_slice_rows_sums_and_leaves_out_none_keys():
    ts = [trip(entry_min=10, entry=100, exit_=102), trip(entry_min=20, entry=100, exit_=99), trip(entry_min=30, side="SHORT", entry=100, exit_=99)]
    rows = scalp.slice_rows(ts, lambda t: t["side"] if t["entry_ts"] < BASE + 25 * MIN else None)
    assert [(r["key"], r["n"]) for r in rows] == [("LONG", 2)]
    assert rows[0]["net"] == round(sum(t["net"] for t in ts[:2]), 2) and rows[0]["wins"] == 1 and rows[0]["losses"] == 1


def test_breakeven_points_is_fees_over_point_value():
    b = scalp.breakeven_points([trip(qty=1)])
    assert b["MNQ"]["fees"] == 0.74 and b["MNQ"]["points"] == 0.37          # 0.74 round-trip fees / $2 per point
    assert scalp.breakeven_points([trip(cid="CON.F.US.ZZZ.Z26")]) == {}     # unknown root: no guess


def test_markdown_withholds_rates_and_marks_small_slices():
    ts = [trip(entry_min=10 * (i + 1), entry=100, exit_=101 if i % 2 else 99) for i in range(6)]
    md = scalp.render_scalp(ts, datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc))
    assert "Cost of doing business" in md and "too few to describe" not in md.split("By time held")[0]
    assert "(too few to describe)" in md or all(r["n"] >= scalp.SMALL for r in scalp.slice_rows(ts, scalp.hold_bucket))
    assert "win rate" not in md.lower().replace("no win rate is shown", "")        # n=6: no win-rate figure anywhere


def test_markdown_empty_ledger_is_one_line():
    assert "No closed round trips" in scalp.render_scalp([], datetime.now(timezone.utc))


def test_trips_table_points_at_the_chart_file():
    t = trip()
    t["ctx"], t["x"] = None, {"ext5": 1.23, "trend5": "with", "chart": scalp.trip_key(t) + ".png", "exc": {"mfe": 5.0, "mae": 1.0, "real": 4.0, "bars": 3, "coarse": False}}
    md = scalp.render_trips([t])
    assert f"charts/{scalp.trip_key(t)}.png" in md and "+5.0 / -1.0" in md and "+1.23" in md


def test_trip_key_is_file_safe():
    k = scalp.trip_key(trip())
    assert k == "20261029-090000-MNQ-LONG" or k.endswith("-MNQ-LONG") and "/" not in k and " " not in k


# ---- chart ----------------------------------------------------------------------------------------------------------------

@pytest.mark.skipif(not tripchart.available(), reason="Pillow not installed")
def test_chart_writes_a_png(tmp_path):
    t = trip(entry_min=100, hold_min=4, entry=28530.0, exit_=28526.0)
    out = tmp_path / "t.png"
    assert tripchart.render_png(t, bars(140), bars(60, unit=5), out, "BIAS with") is True
    blob = out.read_bytes()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n" and 5_000 < len(blob) < 600_000


@pytest.mark.skipif(not tripchart.available(), reason="Pillow not installed")
def test_chart_refuses_too_few_bars_and_open_trips(tmp_path):
    t = trip(entry_min=100)
    assert tripchart.render_png(t, bars(3), bars(2, unit=5), tmp_path / "a.png") is False and not (tmp_path / "a.png").exists()
    t["open"] = True
    assert tripchart.render_png(t, bars(140), bars(60, unit=5), tmp_path / "b.png") is False


def test_5m_ema_only_uses_closed_5m_bars():
    one, five = bars(60), bars(12, unit=5)
    base = tripchart._ema5_by_minute(one, five)
    poisoned = [dict(b) for b in five]
    last = poisoned[-1]
    last.update(close=1e9)
    got = tripchart._ema5_by_minute(one[:55], poisoned)           # the last 5m bar closes at minute 60: not yet closed at minute 55
    assert got == base[:55]


# ---- the report script ----------------------------------------------------------------------------------------------------

def load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("scalp_report", ROOT / "scripts" / "scalp_report.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def desk_transport(calls):
    def handler(req: httpx.Request):
        q = dict(req.url.params)
        calls.append(q)
        n, size = int(q["limit"]), int(q["n"])
        end = int(q["end"])
        rows = [{"timestamp": end - (n - i) * size * MIN, "open": 28500 + i % 5, "high": 28503 + i % 5, "low": 28497 + i % 5, "close": 28501 + i % 5, "volume": 50} for i in range(n)]
        return httpx.Response(200, json=rows)
    return httpx.MockTransport(handler)


def seed_ledger(root, trips_spec):
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=6)
    for k, (off, hold, side, ent, ex) in enumerate(trips_spec):
        for typ, m, price, i in (("in", off, ent, 0), ("out", off + hold, ex, 1)):
            buy = (side == "LONG") == (typ == "in")
            pnl = None if typ == "in" else ((ex - ent) * (1 if side == "LONG" else -1) * 2.0)
            rows.append({"id": 1000 + k * 2 + i, "ts": int((now + timedelta(minutes=m)).timestamp() * 1000), "contract": MNQ, "side": "BUY" if buy else "SELL",
                         "price": price, "size": 1, "pnl": pnl, "fees": 0.37, "order": None})
    (d / "trades.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_script_clusters_nearby_trips_into_one_pair_of_reads(tmp_path, monkeypatch):
    sr = load_script()
    seed_ledger(tmp_path, [(0, 2, "LONG", 28500, 28504), (10, 3, "SHORT", 28510, 28508), (25, 1, "LONG", 28505, 28503)])
    calls = []
    monkeypatch.delenv("PX_MEMORY_DIR", raising=False)
    s = sr.build(tmp_path, 14, charts=False, keep=80, client=httpx.Client(transport=desk_transport(calls)))
    assert s["new"] == 3 and s["fetched"] == 2 and len(calls) == 2 and s["error"] is None
    mem = tmp_path / "data" / "memory"
    assert {f.name for f in mem.iterdir()} >= {"scalp.md", "trips.md", "index.md"}
    assert "Cost of doing business" in (mem / "scalp.md").read_text()


def test_script_second_run_makes_no_requests(tmp_path):
    sr = load_script()
    seed_ledger(tmp_path, [(0, 2, "LONG", 28500, 28504)])
    sr.build(tmp_path, 14, charts=False, keep=80, client=httpx.Client(transport=desk_transport([])))
    calls = []
    s = sr.build(tmp_path, 14, charts=False, keep=80, client=httpx.Client(transport=desk_transport(calls)))
    assert calls == [] and s["new"] == 0 and s["fetched"] == 0


def test_script_desk_down_still_writes_the_numbers(tmp_path):
    sr = load_script()
    seed_ledger(tmp_path, [(0, 2, "LONG", 28500, 28504)])

    def boom(req):
        raise httpx.ConnectError("down")
    s = sr.build(tmp_path, 14, charts=False, keep=80, client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert s["error"] == "ConnectError"
    assert "Cost of doing business" in (tmp_path / "data" / "memory" / "scalp.md").read_text()


@pytest.mark.skipif(not tripchart.available(), reason="Pillow not installed")
def test_script_writes_charts_and_prunes_old_ones(tmp_path):
    sr = load_script()
    seed_ledger(tmp_path, [(0, 2, "LONG", 28500, 28504), (10, 3, "SHORT", 28510, 28508)])
    s = sr.build(tmp_path, 14, charts=True, keep=1, client=httpx.Client(transport=desk_transport([])))
    pngs = list((tmp_path / "data" / "memory" / "charts").glob("*.png"))
    assert s["charts"] == 2 and len(pngs) == 1                      # keep=1: only the newest trip's chart survives


def test_script_never_touches_trades_md_or_notes(tmp_path):
    sr = load_script()
    seed_ledger(tmp_path, [(0, 2, "LONG", 28500, 28504)])
    mem = tmp_path / "data" / "memory"
    mem.mkdir(parents=True)
    (mem / "trades.md").write_text("MINE")
    (mem / "notes.md").write_text("YOURS")
    sr.build(tmp_path, 14, charts=False, keep=80, client=httpx.Client(transport=desk_transport([])))
    assert (mem / "trades.md").read_text() == "MINE" and (mem / "notes.md").read_text() == "YOURS"
