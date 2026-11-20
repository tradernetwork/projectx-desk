"""GET /api/rh/letf, /api/rh/bars, /api/rh/quotes: the LETF desk's read-only routes."""
import httpx
import pytest
from fastapi.testclient import TestClient

from projectx_desk import dev_robinhood
from projectx_desk.rh import letf, reads, state as rh_state
from projectx_desk.rh.mcp import CODES, Client
from tests.test_rh_app import build
from tests.test_rh_reads import Provider

ROUTES = ["/api/rh/letf", "/api/rh/bars?symbols=QQQ&tf=5m", "/api/rh/quotes?symbols=QQQ"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("PX_RH", "PX_RH_MOCK", "PX_RECORD_ONLY", "PX_TRADING", "PX_MCP_PLACE", "PX_MCP_TOKEN", "PX_OPERATOR_TOKEN", "PX_HOST"):
        monkeypatch.delenv(k, raising=False)


def runtime(token=True):
    app = dev_robinhood.make_app()
    st = app.state.mock
    c = Client("http://127.0.0.1/mcp", Provider(st), transport=httpx.ASGITransport(app=app))
    return rh_state.Runtime(reads.Reader(c), token_present=lambda: token, market_quiet=lambda: False), st


def test_routes_absent_without_a_runtime_and_in_record_only():
    for app in (build(), build(rh=runtime()[0], record_only=True)):
        assert not [r.path for r in app.routes if r.path in ("/api/rh/letf", "/api/rh/bars", "/api/rh/quotes")]
    with TestClient(build()) as c:
        for p in ROUTES:
            assert c.get(p).status_code == 404


def test_the_new_routes_are_get_only():
    app = build(rh=runtime()[0])
    mine = [r for r in app.routes if r.path in ("/api/rh/letf", "/api/rh/bars", "/api/rh/quotes")]
    assert len(mine) == 3
    for r in mine:
        assert set(r.methods) <= {"GET", "HEAD"}, r.path


def test_letf_view_serves_the_universe_with_no_outbound_call():
    rt, st = runtime(token=False)
    with TestClient(build(rh=rt)) as c:
        r = c.get("/api/rh/letf")
        j = r.json()
        assert r.status_code == 200 and j["enabled"] is True and j["ok"] is True
        assert len(j["underlyings"]) == 315 and len(j["funds"]) == 694 and j["as_of"] == "2026-11-19" and j["age_days"] >= 0
        assert "name" in j["funds"]["TQQQ"] and set(j["groups"][0]) == {"index", "Broad index"}
    assert st.requests == 0                                           # the list comes from the committed file


def test_no_token_means_no_call_and_a_fixed_code():
    rt, st = runtime(token=False)
    with TestClient(build(rh=rt)) as c:
        assert c.get("/api/rh/bars?symbols=QQQ&tf=5m").json() == {"ok": False, "code": "not_connected"}
        assert c.get("/api/rh/quotes?symbols=QQQ").json() == {"ok": False, "code": "not_connected"}
    assert st.requests == 0


@pytest.mark.parametrize("q,code", [
    ("symbols=QQQ&tf=7m", "bad_tf"), ("symbols=QQQ", "bad_tf"), ("symbols=ZZZZZ&tf=5m", "not_in_universe"),
    ("symbols=QQQ,TQQQ,SQQQ&tf=5m", "too_many"), ("symbols=%3Cscript%3E&tf=5m", "bad_symbol"), ("tf=5m", "bad_symbol"),
    ("symbols=AAA&tf=5m", "not_in_universe"),                          # the mock's own fixed symbols are not in the universe either
])
def test_bars_validation_and_the_universe_restriction(q, code):
    rt, st = runtime()
    with TestClient(build(rh=rt)) as c:
        assert c.get("/api/rh/bars?" + q).json() == {"ok": False, "code": code}


def test_bars_end_to_end_cached_and_both_charts_differ():
    rt, st = runtime()
    with TestClient(build(rh=rt)) as c:
        a = c.get("/api/rh/bars?symbols=QQQ,TQQQ&tf=5m").json()
        assert a["ok"] and a["tf"] == "5m" and a["bar_ms"] == 300000 and a["cached"] is False
        q, t = a["series"]["QQQ"], a["series"]["TQQQ"]
        assert q["n"] == len(q["bars"]) > 100 and t["n"] > 100 and q["bars"][0] != t["bars"][0]
        assert all(len(b) == 6 for b in q["bars"])
        n = len(st.hist_calls)
        b = c.get("/api/rh/bars?symbols=qqq,tqqq&tf=5m").json()
        assert b["cached"] is True and len(st.hist_calls) == n          # the second request made no call
        assert st.hist_calls[0]["bounds"] == "regular" and "account_number" not in st.hist_calls[0]
        d = c.get("/api/rh/bars?symbols=QQQ,TQQQ&tf=15m").json()
        assert d["ok"] and d["bar_ms"] == 900000 and len(st.hist_calls) == n + 2    # 15m: one call per symbol, three bars made one


def test_quotes_route_and_ttl():
    rt, st = runtime()
    with TestClient(build(rh=rt)) as c:
        a = c.get("/api/rh/quotes?symbols=QQQ,TQQQ").json()
        assert a["ok"] and a["quotes"]["TQQQ"]["bid"] < a["quotes"]["TQQQ"]["ask"] and a["quotes"]["QQQ"]["prev_close"] > 0
        n = st.calls.count("get_equity_quotes")
        assert c.get("/api/rh/quotes?symbols=QQQ,TQQQ").json()["cached"] is True
        assert st.calls.count("get_equity_quotes") == n
        assert c.get("/api/rh/quotes?symbols=QQQ,TQQQ,SQQQ,SPXL,UPRO").json() == {"ok": False, "code": "too_many"}


def test_busy_when_the_budget_is_spent():
    rt, st = runtime()
    app = build(rh=rt)
    app.state.rh_market = letf.MarketCache(per_s=0.0, burst=0.0)
    with TestClient(app) as c:
        assert c.get("/api/rh/bars?symbols=QQQ&tf=5m").json() == {"ok": False, "code": "busy"}
    assert st.hist_calls == []


def test_errors_are_fixed_codes_never_provider_text():
    rt, st = runtime()
    st.fault = "500"
    with TestClient(build(rh=rt)) as c:
        j = c.get("/api/rh/bars?symbols=QQQ&tf=5m").json()
        assert j["ok"] is False and set(j) == {"ok", "code"} and j["code"] in set(CODES)
        j = c.get("/api/rh/quotes?symbols=QQQ").json()
        assert j["ok"] is False and set(j) == {"ok", "code"} and j["code"] in set(CODES)


def test_unusable_file_answers_letf_unavailable():
    rt, _ = runtime()
    app = build(rh=rt)
    app.state.rh_letf = False
    with TestClient(app) as c:
        assert c.get("/api/rh/letf").json() == {"enabled": True, "ok": False, "code": "letf_unavailable"}
        assert c.get("/api/rh/bars?symbols=QQQ&tf=5m").json() == {"ok": False, "code": "letf_unavailable"}
        assert c.get("/api/rh/quotes?symbols=QQQ").json() == {"ok": False, "code": "letf_unavailable"}


def test_letf_never_reaches_the_ai_surface(monkeypatch):
    rt, _ = runtime()
    with TestClient(build(rh=rt)) as c:
        c.get("/api/rh/letf")
        for p in ("/ai/digest", "/ai/state"):
            r = c.get(p)
            assert "TQQQ" not in r.text and "letf" not in r.text.lower()


def test_movers_route_scans_the_top_funds_in_batches_and_caches():
    rt, st = runtime()
    app = build(rh=rt)
    app.state.rh_market = letf.MarketCache(per_s=100.0, burst=100.0)
    with TestClient(app) as c:
        base = st.calls.count("get_equity_quotes")
        j = c.get("/api/rh/movers").json()
        assert j["ok"] and j["scan_n"] == 120 and len(j["rows"]) == 120 and j["n_quoted"] >= 100
        made = st.calls.count("get_equity_quotes") - base
        assert 1 <= made <= 10                                        # about 175 symbols in batches of 20, no account call
        row = next(r for r in j["rows"] if r["s"] == "TQQQ")
        assert abs(row["gap_bps"]) < 5 and abs(row["move"] - row["lev"] * row["u_move"]) < 1e-3     # the mock fund is daily-reset 3x
        assert c.get("/api/rh/movers").json()["ok"]
        assert st.calls.count("get_equity_quotes") - base == made    # the second scan came from the cache


def test_movers_busy_and_no_token():
    rt, st = runtime()
    app = build(rh=rt)
    app.state.rh_market = letf.MarketCache(per_s=0.0, burst=0.0)
    with TestClient(app) as c:
        assert c.get("/api/rh/movers").json() == {"ok": False, "code": "busy"}
    rt2, st2 = runtime(token=False)
    with TestClient(build(rh=rt2)) as c:
        assert c.get("/api/rh/movers").json() == {"ok": False, "code": "not_connected"}
    assert st2.requests == 0
