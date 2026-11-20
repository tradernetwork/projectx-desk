"""projectx_desk/rh/letf.py: the LETF desk's universe loader, chart ranges, symbol allowlist, cache and response builders."""
import ast
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from projectx_desk.rh import guard as rh_guard, letf, mcp
from projectx_desk.rh.mcp import McpError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def uni():
    u = letf.load()
    assert u is not None
    return u


def test_tf_ranges_stay_under_the_reply_cap():
    for tf, spec in letf.TF.items():
        est = letf.est_bars(tf)
        assert est <= 1600, tf
        if spec["k"] == 1:
            assert est <= letf.MAX_BARS_PER_SYMBOL, tf              # about 800 bars a symbol keeps a reply under 1 MB
    assert letf.est_bars("15m") // letf.TF["15m"]["k"] == 520         # 1560 five-minute bars become 520 drawn bars


def test_split_calls_policy():
    assert letf.split_calls(["QQQ", "TQQQ"], "5m") == [["QQQ", "TQQQ"]]
    assert letf.split_calls(["QQQ", "TQQQ"], "1D") == [["QQQ", "TQQQ"]]
    assert letf.split_calls(["QQQ", "TQQQ"], "15m") == [["QQQ"], ["TQQQ"]]     # 1560 x 2 would be over the cap
    assert letf.split_calls(["QQQ"], "15m") == [["QQQ"]]


def test_range_for_counts_weekdays_back_from_utc_midnight():
    wed = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    assert letf.range_for("1m", wed) == ("2026-11-16T00:00:00Z", 780)
    sat = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    assert letf.range_for("1m", sat)[0] == "2026-11-19T00:00:00Z"
    start, est = letf.range_for("1D", wed)
    assert est == 500 and datetime.fromisoformat(start.replace("Z", "+00:00")) < wed - timedelta(days=500)


@pytest.mark.parametrize("text,code", [
    (None, "bad_symbol"), ("", "bad_symbol"), ("QQQ,", "bad_symbol"), ("qq q", "bad_symbol"), ("QQQ;DROP", "bad_symbol"),
    ("A" * 70, "bad_symbol"), ("QQQ,TQQQ,SQQQ", "too_many"), ("ZZZZZ", "not_in_universe"), ("QQQ,ZZZZZ", "not_in_universe"),
])
def test_parse_symbols_codes(uni, text, code):
    with pytest.raises(letf.LetfError) as e:
        letf.parse_symbols(text, uni, 2)
    assert e.value.code == code


def test_parse_symbols_normalises_and_dedups(uni):
    assert letf.parse_symbols(" qqq , TQQQ,qqq", uni, 2) == ["QQQ", "TQQQ"]
    assert "GLD" in uni.allowed and "UGL" in uni.allowed             # chart symbols and funds are both allowed
    assert "Gold" not in uni.allowed and "HYPE" not in uni.allowed    # a label never charts itself


def _b(t, o=10.0, h=11.0, l=9.0, c=10.5, v=100.0):
    return [t, o, h, l, c, v]


def test_aggregate_5m_to_15m_alignment_and_forming():
    t0 = int(datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc).timestamp() * 1000)
    five = 300_000
    bars = [_b(t0 + i * five, o=10 + i, h=20 + i, l=1 + i, c=11 + i, v=10) for i in range(7)]   # 14:30 .. 15:00
    rows, forming = letf.aggregate(bars, 3, five)
    assert [r[0] for r in rows] == [t0, t0 + 3 * five, t0 + 6 * five]                          # 14:30, 14:45, 15:00 buckets
    assert rows[0][1:] == [10, 22, 1, 13, 30] and forming is True                                # last bucket holds one of three
    rows, forming = letf.aggregate(bars[:6], 3, five)
    assert forming is False and len(rows) == 2
    assert letf.aggregate(bars, 1, five) == (bars, False)


def test_aggregate_unknown_volume_stays_unknown():
    t0 = 1_795_098_600_000
    rows, _ = letf.aggregate([_b(t0, v=5), _b(t0 + 300_000, v=None), _b(t0 + 600_000, v=5)], 3, 300_000)
    assert rows[0][5] is None


def test_equity_session_open_matches_the_guards():
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for i in range(0, 14 * 24 * 60, 7):                              # every 7 minutes for two weeks, across a DST change
        now = t + timedelta(minutes=i)
        assert letf.equity_session_open(now) == rh_guard.market_open_now(now)
    assert letf.equity_session_open(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)) is True     # 10:00 ET Thursday
    assert letf.equity_session_open(datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)) is False    # Saturday


def test_codes_stay_equal_to_the_mcp_clients():
    assert letf.MCP_CODES == set(mcp.CODES)


def test_module_imports_no_guard_client_hub_mcp_or_app():
    tree = ast.parse((ROOT / "projectx_desk" / "rh" / "letf.py").read_text(encoding="utf-8"))
    banned = {"guard", "client", "hub", "mcp", "app", "reads", "state", "chat"}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mods = set((n.module or "").split(".")) | {a.name for a in n.names}
            assert not (mods & banned), n.module
        elif isinstance(n, ast.Import):
            assert not ({p for a in n.names for p in a.name.split(".")} & banned)


# ---- the universe ------------------------------------------------------------------------------------------------------------------
def test_real_universe_loads_and_public_view_has_age(uni):
    v = uni.public_view(date(2026, 11, 29))
    assert v["ok"] and v["as_of"] == "2026-11-19" and v["age_days"] == 10 and len(v["underlyings"]) == 315
    assert uni.public_view(date(2026, 11, 1))["age_days"] == 0       # never negative


def _mutated(tmp_path, fn):
    d = json.loads(letf.DEFAULT_PATH.read_text(encoding="utf-8"))
    fn(d)
    p = tmp_path / "x.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return letf.load(p)


@pytest.mark.parametrize("fn", [
    lambda d: d.update(version=3),
    lambda d: d.update(as_of="yesterday"),
    lambda d: d["funds"]["TQQQ"].update(lev=float("nan")),
    lambda d: d["funds"]["TQQQ"].update(lev=9.0),
    lambda d: d["funds"]["TQQQ"].update(dv20=-1),
    lambda d: d["funds"]["TQQQ"].update(name="<img src=x onerror=alert(1)>"),
    lambda d: d["funds"].update({"bad sym": d["funds"]["TQQQ"]}),
    lambda d: d["underlyings"][0].update(chart="not a ticker"),
    lambda d: d["underlyings"][0].update(chart_kind="weird"),
    lambda d: d["underlyings"][0].update(chart=None, chart_kind="self"),
    lambda d: d["underlyings"][0].update(long_pick="NOPE"),
    lambda d: d["underlyings"].append(dict(d["underlyings"][0])),
    lambda d: d.update(groups=[]),
])
def test_tampered_universe_fails_closed(tmp_path, fn):
    assert _mutated(tmp_path, fn) is None


def test_missing_or_garbled_file_is_none(tmp_path):
    assert letf.load(tmp_path / "nope.json") is None
    (tmp_path / "g.json").write_text("{not json", encoding="utf-8")
    assert letf.load(tmp_path / "g.json") is None


# ---- the cache ---------------------------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


async def test_cache_ttl_and_lru():
    clk = Clock()
    c = letf.MarketCache(clock=clk, per_s=1000, burst=1000, max_entries=2)
    n = []

    async def f(v):
        n.append(v)
        return v
    assert await c.get("a", 10, lambda: f(1)) == (1, False)
    assert await c.get("a", 10, lambda: f(2)) == (1, True)            # cached
    clk.t += 11
    assert await c.get("a", 10, lambda: f(3)) == (3, False)           # expired
    await c.get("b", 10, lambda: f("b"))
    await c.get("c", 10, lambda: f("c"))                              # evicts the least recently used ("a")
    assert await c.get("a", 10, lambda: f(4)) == (4, False)
    assert n == [1, 3, "b", "c", 4]


async def test_single_flight_one_fetch_for_two_tabs():
    c = letf.MarketCache(per_s=1000, burst=1000)
    gate, n = asyncio.Event(), []

    async def slow():
        n.append(1)
        await gate.wait()
        return "v"
    a = asyncio.create_task(c.get("k", 5, slow))
    await asyncio.sleep(0)
    b = asyncio.create_task(c.get("k", 5, slow))
    await asyncio.sleep(0)
    gate.set()
    assert await a == ("v", False) and await b == ("v", True) and n == [1]


async def test_failure_is_not_cached_and_reaches_waiters():
    c = letf.MarketCache(per_s=1000, burst=1000)
    gate = asyncio.Event()

    async def bad():
        await gate.wait()
        raise McpError("timeout")
    a = asyncio.create_task(c.get("k", 5, bad))
    await asyncio.sleep(0)
    b = asyncio.create_task(c.get("k", 5, bad))
    await asyncio.sleep(0)
    gate.set()
    for t in (a, b):
        with pytest.raises(McpError):
            await t

    async def good():
        return 1
    assert await c.get("k", 5, good) == (1, False)                    # the failure left nothing behind


async def test_budget_busy_then_refills():
    clk = Clock()
    c = letf.MarketCache(clock=clk, per_s=2.0, burst=4.0)

    async def f():
        return 1
    for i in range(4):
        await c.get(("k", i), 1, f)                                   # the burst of 4
    with pytest.raises(letf.LetfError) as e:
        await c.get(("k", 99), 1, f)
    assert e.value.code == "busy"
    assert await c.get(("k", 0), 1, f) == (1, True)                   # a cache hit costs nothing
    clk.t += 1.0                                                      # two tokens back
    await c.get(("k", 100), 1, f)
    await c.get(("k", 101), 1, f)
    with pytest.raises(letf.LetfError):
        await c.get(("k", 102), 1, f)


# ---- response builders with a fake reader ------------------------------------------------------------------------------------------
class FakeReader:
    def __init__(self):
        self.bar_calls, self.quote_calls = [], []
        self.raise_with = None

    async def bars(self, symbols, interval, start):
        self.bar_calls.append((list(symbols), interval, start))
        if self.raise_with:
            raise self.raise_with
        t0 = 1_795_098_600_000
        return {s: {"bars": [_b(t0 + i * 300_000) for i in range(6)], "dropped": 1} for s in symbols if s != "GLD"}

    async def quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {"QQQ": {"bid": 1.0, "ask": 1.1, "last": 1.05, "prev_close": None, "updated_at": 1795098600.5}}


NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)


async def test_bars_response_shape_and_split(uni):
    r, c = FakeReader(), letf.MarketCache()
    out = await letf.bars_response(r, uni, c, "qqq,tqqq", "5m", True, NOW)
    assert out["ok"] and out["tf"] == "5m" and out["bar_ms"] == 300_000 and out["session_open"] is True and out["cached"] is False
    assert out["series"]["QQQ"]["n"] == 6 and out["series"]["QQQ"]["dropped"] == 1
    assert r.bar_calls == [(["QQQ", "TQQQ"], "5minute", "2026-11-05T00:00:00Z")]
    out15 = await letf.bars_response(FakeReader(), uni, letf.MarketCache(), "QQQ,TQQQ", "15m", True, NOW)
    assert out15["bar_ms"] == 900_000 and out15["series"]["QQQ"]["n"] == 2 and out15["series"]["QQQ"]["forming"] is False


async def test_bars_response_missing_symbol_is_empty_not_absent(uni):
    out = await letf.bars_response(FakeReader(), uni, letf.MarketCache(), "GLD", "5m", True, NOW)
    assert out["series"]["GLD"] == {"n": 0, "dropped": 0, "bars": []}


@pytest.mark.parametrize("args,code", [
    (("QQQ", "7m", True), "bad_tf"), (("ZZZZZ", "5m", True), "not_in_universe"), (("QQQ,TQQQ,SQQQ", "5m", True), "too_many"),
    (("bad sym", "5m", True), "bad_symbol"), (("QQQ", "5m", False), "not_connected"),
])
async def test_bars_response_refusal_codes_make_no_call(uni, args, code):
    r = FakeReader()
    assert await letf.bars_response(r, uni, letf.MarketCache(), *args, NOW) == {"ok": False, "code": code}
    assert r.bar_calls == []


async def test_errors_are_fixed_codes_never_text(uni):
    r = FakeReader()
    r.raise_with = McpError("rate_limited")
    assert await letf.bars_response(r, uni, letf.MarketCache(), "QQQ", "5m", True, NOW) == {"ok": False, "code": "rate_limited"}
    r.raise_with = RuntimeError("SECRET-PROVIDER-TEXT account 12345")
    out = await letf.bars_response(r, uni, letf.MarketCache(), "QQQ", "5m", True, NOW)
    assert out == {"ok": False, "code": "read_failed"} and "SECRET" not in json.dumps(out)


async def test_quotes_response_lists_unknown_never_zero(uni):
    r = FakeReader()
    out = await letf.quotes_response(r, uni, letf.MarketCache(), "QQQ,TQQQ", True, NOW)
    q = out["quotes"]
    assert q["QQQ"]["last"] == 1.05 and q["QQQ"]["updated_ms"] == 1795098600500 and q["QQQ"]["unknown"] == ["prev_close"]
    assert "prev_close" not in q["QQQ"]                               # absent, not 0
    assert q["TQQQ"]["unknown"] == ["bid", "ask", "last", "prev_close", "updated_ms"]
    assert r.quote_calls == [["QQQ", "TQQQ"]]
    out2 = await letf.quotes_response(r, uni, letf.MarketCache(), "A", True, NOW)
    assert out2 == {"ok": False, "code": "not_in_universe"}


async def test_bars_cached_second_call_makes_no_reader_call(uni):
    r, c = FakeReader(), letf.MarketCache()
    await letf.bars_response(r, uni, c, "QQQ", "5m", True, NOW)
    again = await letf.bars_response(r, uni, c, "QQQ", "5m", True, NOW)
    assert again["cached"] is True and len(r.bar_calls) == 1


# ---- the movers scan -----------------------------------------------------------------------------------------------------------------
class ScanReader:
    """Every symbol quoted at 100 with a previous close of 100, except where `moves` says otherwise."""

    def __init__(self, moves=None, fail_after=None):
        self.calls, self.moves, self.fail_after = [], moves or {}, fail_after

    async def quotes(self, symbols):
        self.calls.append(list(symbols))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise McpError("rate_limited")
        return {s: {"bid": 99.9, "ask": 100.1, "last": 100.0 * (1 + self.moves.get(s, 0.0)), "prev_close": 100.0, "updated_at": 1795098600.0}
                for s in symbols}


def test_scan_plan_is_top_by_dollar_volume_batched_and_stable(uni):
    funds, batches = letf.scan_plan(uni)
    assert len(funds) == letf.SCAN_N == 120
    dv = [f["dv20"] if f["dv20"] is not None else -1 for _, f in funds]
    assert dv == sorted(dv, reverse=True)
    flat = [s for b in batches for s in b]
    assert len(flat) == len(set(flat)) and all(len(b) <= letf.SCAN_BATCH for b in batches) and all(s in uni.allowed for s in flat)
    assert all(s in flat for s, _ in funds)                           # every scanned fund is in some batch
    assert letf.scan_plan(uni)[1] == batches                          # same order next time, so cache keys repeat


def test_mover_row_gap_unknown_and_split_rules():
    f = {"u": "Nasdaq 100", "lev": 3.0, "dv20": 5e9}
    r = letf.mover_row("TQQQ", f, {"last": 94.0, "prev_close": 100.0, "bid": 93.9, "ask": 94.1}, {"last": 98.0, "prev_close": 100.0})
    assert r["move"] == pytest.approx(-0.06) and r["u_move"] == pytest.approx(-0.02) and r["gap_bps"] == pytest.approx(0.0, abs=1e-6)
    assert r["spread_bps"] == pytest.approx(21.28, abs=0.1)
    r = letf.mover_row("TQQQ", f, {"last": 90.0, "prev_close": 100.0}, {"last": 98.0, "prev_close": 100.0})
    assert r["gap_bps"] == pytest.approx(-400.0)
    for q, uq in ((None, {"last": 98.0, "prev_close": 100.0}), ({"last": 90.0}, {"last": 98.0, "prev_close": 100.0}),
                  ({"last": 90.0, "prev_close": 100.0}, None), ({"last": 90.0, "prev_close": 100.0}, {"last": 98.0})):
        r = letf.mover_row("TQQQ", f, q, uq)
        assert r["gap_bps"] is None                                   # unknown is None, never 0
    r = letf.mover_row("TQQQ", f, {"last": 30.0, "prev_close": 100.0}, {"last": 99.0, "prev_close": 100.0})
    assert r["move"] is None and r["gap_bps"] is None                 # -70% that 3x of -1% cannot explain: a corporate action, not a gap


async def test_movers_response_rows_and_one_call_per_batch(uni):
    funds, batches = letf.scan_plan(uni)
    top = funds[0][0]
    r = ScanReader(moves={top: -0.03})
    out = await letf.movers_response(r, uni, letf.MarketCache(per_s=100.0, burst=100.0), True, NOW)
    assert out["ok"] and out["scan_n"] == 120 and len(out["rows"]) == 120 and out["list_as_of"] == uni.doc["as_of"]
    assert len(r.calls) == len(batches) and out["session_open"] is True and out["n_quoted"] == 120
    row = next(x for x in out["rows"] if x["s"] == top)
    assert row["move"] == pytest.approx(-0.03)


async def test_movers_busy_keeps_the_batches_already_fetched(uni):
    _, batches = letf.scan_plan(uni)
    clk = type("C", (), {"t": 0.0})()
    cache = letf.MarketCache(clock=lambda: clk.t, per_s=2.0, burst=4.0)
    r = ScanReader()
    assert await letf.movers_response(r, uni, cache, True, NOW) == {"ok": False, "code": "busy"}
    assert len(r.calls) == 4                                          # the burst
    out = None
    for _ in range(5):                                                # the client retries every 3 s, inside the 30 s TTL
        clk.t += 3.0
        out = await letf.movers_response(r, uni, cache, True, NOW)
        if out["ok"]:
            break
    assert out["ok"] and len(r.calls) == len(batches)                 # every batch fetched exactly once across the retries


async def test_movers_refusals_make_no_call_and_errors_are_fixed_codes(uni):
    r = ScanReader()
    assert await letf.movers_response(r, uni, letf.MarketCache(), False, NOW) == {"ok": False, "code": "not_connected"}
    assert r.calls == []
    out = await letf.movers_response(ScanReader(fail_after=0), uni, letf.MarketCache(per_s=100.0, burst=100.0), True, NOW)
    assert out == {"ok": False, "code": "rate_limited"}


async def test_movers_ttl_is_longer_when_the_market_is_closed(uni):
    r, c = ScanReader(), letf.MarketCache(per_s=100.0, burst=100.0)
    sat = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    await letf.movers_response(r, uni, c, True, sat)
    n = len(r.calls)
    c.clock = lambda: c._last + 300.0                                  # five minutes later: still cached on a closed market
    await letf.movers_response(r, uni, c, True, sat)
    assert len(r.calls) == n


def test_bad_issuer_reads_as_null_not_a_failed_file(tmp_path):
    u = _mutated(tmp_path, lambda d: d["funds"]["TQQQ"].update(issuer="<img src=x>"))
    assert u is not None and u.public_view(date(2026, 11, 20))["funds"]["TQQQ"]["issuer"] is None
    u = _mutated(tmp_path, lambda d: d["funds"]["TQQQ"].update(issuer=7))
    assert u is not None and u.public_view(date(2026, 11, 20))["funds"]["TQQQ"]["issuer"] is None


def test_real_universe_has_issuers(uni):
    assert uni.public_view(date(2026, 11, 20))["funds"]["TQQQ"]["issuer"] == "ProShares"
