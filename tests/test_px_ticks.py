import math

import px_ticks
from px_ticks import TickBars, parse_sizes, MAX_BARS, MAX_CONTRACTS


def feed(tb, cid, rows):
    for r in rows:
        tb.add(cid, *r)


def test_boundaries_ohlc_and_sums():
    tb = TickBars(sizes=(3,))
    feed(tb, "C", [(1000, 10.0, 2, 0), (1001, 12.0, 1, 1), (1002, 9.0, 4, 7),     # bar 1
                   (1003, 11.0, 5, 0), (1004, 11.5, 1, 1), (1005, 8.0, 2, None),   # bar 2
                   (1006, 20.0, 3, 0)])                                            # forming
    s = tb.snapshot("C", 3)
    assert s["v"] == 1 and s["n"] == 3 and s["since"] == 1000
    assert s["bars"] == [
        [1000, 10.0, 12.0, 9.0, 9.0, 7, 2, 1, 0, 3],
        [1003, 11.0, 11.5, 8.0, 8.0, 8, 5, 1, 0, 3],
        [1006, 20.0, 20.0, 20.0, 20.0, 3, 3, 0, 0, 1],
    ]


def test_unknown_side_counted_apart_and_not_in_buy_sell():
    tb = TickBars(sizes=(50,))
    feed(tb, "C", [(1, 1.0, 5, 9), (2, 1.0, 2, "0"), (3, 1.0, 1, 0)])
    b = tb._c["C"]["cur"][50]
    assert (b["buy"], b["sell"], b["unk"], b["v"]) == (1, 0, 7, 8)


def test_gap_from_mark_gap_and_malformed_print():
    tb = TickBars(sizes=(3,))
    feed(tb, "C", [(1, 1.0, 1, 0)])
    tb.mark_gap("C")
    feed(tb, "C", [(2, 1.0, 1, 0), (3, 1.0, 1, 0), (4, 1.0, 1, 0)])
    rows = tb.snapshot("C", 3)["bars"]
    assert rows[0][8] == 1 and rows[0][9] == 3 and rows[1][8] == 0
    tb2 = TickBars(sizes=(3,))
    feed(tb2, "C", [(1, 1.0, 1, 0), (2, float("nan"), 1, 0), (3, 1.0, 1, 0)])
    r = tb2.snapshot("C", 3)["bars"][0]
    assert r[8] == 1 and r[9] == 2          # flagged, and the bad print was not counted


def test_gap_with_no_forming_bar_hits_next_bar():
    tb = TickBars(sizes=(2,))
    feed(tb, "C", [(1, 1.0, 1, 0), (2, 1.0, 1, 0)])
    tb.mark_gap("C")
    feed(tb, "C", [(3, 1.0, 1, 0)])
    bars = tb.snapshot("C", 2)["bars"]
    assert bars[0][8] == 0 and bars[1][8] == 1


def test_hostile_inputs():
    tb = TickBars(sizes=(50,))
    tb.add("C", 1, 1.0, 1, 0)
    for bad in [(True, 1.0, 1, 0), (2, True, 1, 0), (2, 1.0, True, 0), (2, "1", 1, 0), (2, 1.0, "1", 0),
                (2, math.inf, 1, 0), (2, 1.0, math.nan, 0), (2, -1.0, 1, 0), (2, 0, 1, 0), (2, 1.0, 0, 0),
                (2, 1.0, -3, 0), (2, 1.0, 10**9, 0), (None, 1.0, 1, 0), (math.inf, 1.0, 1, 0), (2, None, 1, 0)]:
        tb.add("C", *bad)
    s = tb.snapshot("C", 50)
    assert s["bars"][0][9] == 1 and s["bars"][0][8] == 1
    TickBars(sizes=(50,)).add("X", "a", "b", "c", "d")        # never raises, never creates state
    assert TickBars(sizes=(50,)).snapshot("X", 50)["bars"] == []


def test_out_of_order_timestamp_tolerated():
    tb = TickBars(sizes=(3,))
    feed(tb, "C", [(100, 1.0, 1, 0), (90, 2.0, 1, 0), (110, 3.0, 1, 0)])
    r = tb._c["C"]["done"][3][0]
    assert r["t"] == 100 and r["t_end"] == 110 and r["o"] == 1.0 and r["c"] == 3.0


def test_add_trade_never_raises():
    tb = TickBars(sizes=(50,))
    tb.add_trade("C", {"timestamp": 5, "price": 1.0, "volume": 1, "type": 0}, lambda x: x)
    tb.add_trade("C", {"volume": 1}, lambda x: x)
    tb.add_trade("C", None, lambda x: x)
    r = tb.snapshot("C", 50)["bars"][0]
    assert r[9] == 1 and r[8] == 1


def test_env_parsing(monkeypatch):
    assert parse_sizes("") == (1024, 2048, 4096)
    assert parse_sizes("abc, ,x1") == (1024, 2048, 4096)
    assert parse_sizes("500, 500, 1000") == (500, 1000)
    assert parse_sizes("1,99999,300") == (50, 50000, 300)
    assert parse_sizes("100,200,300,400,500") == (1024, 2048, 4096)       # more than 4 distinct -> default
    assert parse_sizes("100,junk,200") == (100, 200)
    monkeypatch.setenv("PX_TICK_BARS", "250,750")
    assert TickBars().sizes == (250, 750)
    monkeypatch.delenv("PX_TICK_BARS")
    assert TickBars().sizes == px_ticks.DEFAULT_SIZES


def test_bar_cap_and_contract_cap():
    tb = TickBars(sizes=(1,))
    for i in range(MAX_BARS + 50):
        tb.add("C", i + 1, 1.0, 1, 0)
    assert len(tb._c["C"]["done"][1]) == MAX_BARS
    assert tb.snapshot("C", 1, limit=10**6)["bars"][-1][0] == MAX_BARS + 50
    for i in range(MAX_CONTRACTS + 2):
        tb.add(f"K{i}", 1, 1.0, 1, 0)
    assert len(tb._c) <= MAX_CONTRACTS and "C" not in tb._c and f"K{MAX_CONTRACTS + 1}" in tb._c


def test_snapshot_limit_and_unknown_n():
    tb = TickBars(sizes=(1, 2))
    for i in range(10):
        tb.add("C", i + 1, 1.0, 1, 0)
    assert [b[0] for b in tb.snapshot("C", 1, limit=3)["bars"]] == [8, 9, 10]
    assert len(tb.snapshot("C", 2, limit=2)["bars"]) == 2
    assert tb.snapshot("C", 777) == {"v": 1, "n": None, "since": None, "bars": []}
    assert tb.snapshot("nope", 1)["bars"] == [] and tb.snapshot("nope", 1)["since"] is None
    assert len(tb.snapshot("C", 1, limit="junk")["bars"]) == 10
    assert len(tb.snapshot("C", 1, limit=0)["bars"]) == 1


def test_route_and_state_hook(monkeypatch):
    monkeypatch.setenv("PX_TICK_BARS", "50,100")
    from fastapi.testclient import TestClient
    from tests.test_px_app import app_and_fake
    app, _ = app_and_fake()
    with TestClient(app, base_url="http://testserver") as c:
        live = app.state.live
        live.ticks = TickBars(sizes=(2, 5))
        cid = "CON.F.US.MES.Z25"
        for i, (p, v, ty) in enumerate([(10.0, 2, 0), (11.0, 3, 1), (12.0, 1, 0)]):
            live._on_trade(cid, {"timestamp": f"2025-01-02T15:00:0{i}+00:00", "volume": v, "type": ty, "price": p})
        live._on_trade(cid, {"timestamp": "2025-01-02T15:00:09+00:00", "volume": 1, "type": 0})   # no price: gap, uncounted
        assert c.get("/api/tickbars/config").json() == {"v": 1, "sizes": [2, 5]}
        j = c.get("/api/tickbars", params={"contract": cid, "n": 2}).json()
        assert j["v"] == 1 and j["n"] == 2 and j["since"] is not None
        assert [r[1:] for r in j["bars"]] == [[10.0, 11.0, 10.0, 11.0, 5, 2, 3, 0, 2], [12.0, 12.0, 12.0, 12.0, 1, 1, 0, 1, 1]]
        assert len(c.get("/api/tickbars", params={"contract": cid, "n": 2, "limit": 1}).json()["bars"]) == 1
        assert c.get("/api/tickbars", params={"contract": cid, "n": 3}).status_code == 400
        assert c.get("/api/tickbars", params={"contract": "bad contract!", "n": 2}).status_code in (400, 422)
        live.ticks.mark_gap(None)
        assert c.get("/api/tickbars", params={"contract": cid, "n": 5}).json()["bars"][-1][8] == 1


def test_parse_sizes_survives_unicode_digits_and_garbage():                                 # fuzz pass finding
    from px_ticks import DEFAULT_SIZES, parse_sizes
    assert parse_sizes("\u00b2,1024") == (1024,) and parse_sizes("\u00b2") == DEFAULT_SIZES
