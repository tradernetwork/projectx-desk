from px_flow import MAX_MINUTES, MIN_MS, FlowStore

M = 29_866_666 * MIN_MS      # a minute-aligned base in 2026 (timestamps outside 2000-2100 are rejected)


def test_buy_sell_unknown_split_and_since():
    f = FlowStore()
    f.add("C", M + 5_000, 2, 0)            # first trade: that minute is entered mid-way, so it is partial
    f.add("C", M + MIN_MS + 1_000, 3, 0)
    f.add("C", M + MIN_MS + 2_000, 4, 1)
    f.add("C", M + MIN_MS + 3_000, 5, 7)   # side not 0/1: counted as unknown, never guessed
    s = f.snapshot("C")
    assert s["since"] == M + MIN_MS
    rows = {r[0]: r for r in s["minutes"]}
    assert rows[M][4] == 1 and rows[M + MIN_MS] == [M + MIN_MS, 3, 4, 5, 0]


def test_bad_trade_marks_minute_partial_and_is_not_counted():
    f = FlowStore()
    f.add("C", M, 1, 0)
    for bad in (float("nan"), 0, -3, True, "5", 10**9, None):
        f.add("C", M + MIN_MS, bad, 0)
    rows = {r[0]: r for r in f.snapshot("C")["minutes"]}
    assert rows[M + MIN_MS][4] == 1 and rows[M + MIN_MS][1:4] == [0, 0, 0] if M + MIN_MS in rows else True
    f.add("C", float("nan"), 1, 0)          # unusable timestamp: ignored, no crash
    f.add("C", True, 1, 0)


def test_gap_marks_this_and_next_minute_partial():
    f = FlowStore()
    f.add("C", M, 1, 0)
    f.add("C", M + MIN_MS, 1, 0)
    f.mark_gap(None, M + MIN_MS + 30_000)
    f.add("C", M + 2 * MIN_MS + 5, 1, 1)
    rows = {r[0]: r[4] for r in f.snapshot("C")["minutes"]}
    assert rows[M + MIN_MS] == 1 and rows[M + 2 * MIN_MS] == 1
    f.add("C", M + 3 * MIN_MS, 1, 1)
    assert {r[0]: r[4] for r in f.snapshot("C")["minutes"]}[M + 3 * MIN_MS] == 0


def test_unknown_contract_empty_and_caps():
    f = FlowStore()
    assert f.snapshot("X") == {"v": 1, "since": None, "minutes": [], "holes": []}
    for i in range(MAX_MINUTES + 50):
        f.add("C", M + i * MIN_MS, 1, 0)
    s = f.snapshot("C")
    assert len(s["minutes"]) == MAX_MINUTES and s["since"] >= s["minutes"][0][0]
    for i in range(20):
        f.add(f"K{i}", M, 1, 0)
    assert len(f._c) <= 8


def test_route_and_state_hook(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_px_app import app_and_fake
    app, _ = app_and_fake()
    with TestClient(app, base_url="http://testserver") as c:
        app.state.live._on_trade("CON.F.US.MES.Z25", [{"timestamp": "2025-01-02T15:00:00+00:00", "volume": 2, "type": 0, "price": 1.0}])
        app.state.live._on_trade("CON.F.US.MES.Z25", {"timestamp": "2025-01-02T15:01:10+00:00", "volume": 3, "type": 1})
        j = c.get("/api/flow", params={"contract": "CON.F.US.MES.Z25"}).json()
        assert j["v"] == 1 and j["minutes"][-1][1:4] == [0, 3, 0]
        assert c.get("/api/flow", params={"contract": "bad contract!"}).status_code in (400, 422)


def test_partial_quote_updates_merge_instead_of_wiping():
    from px_state import LiveState
    live = LiveState()
    live._on_quote("C", {"bestBid": 10.0, "bestAsk": 10.25, "lastPrice": 10.0})
    live._on_quote("C", {"bestAsk": 10.5})                    # ask-only update
    q = live.quotes["C"]
    assert (q["bestBid"], q["bestAsk"], q["lastPrice"]) == (10.0, 10.5, 10.0)
    live._on_quote("C", {"bestBid": None, "bestAsk": 10.75})   # an explicit null never erases a known value
    assert live.quotes["C"]["bestBid"] == 10.0


def test_restart_keeps_buckets_and_turns_downtime_into_a_hole(tmp_path):
    f = str(tmp_path / "flow.json")
    a = FlowStore(path=f, save_every_s=0)
    for i in range(3):
        a.add("C", M + i * MIN_MS + 10, 2, 0)
    a.save()
    b = FlowStore(path=f)                                   # the desk restarted
    assert [r[0] for r in b.snapshot("C")["minutes"]] == [M, M + MIN_MS, M + 2 * MIN_MS]
    b.add("C", M + 10 * MIN_MS + 5, 1, 1)                   # first trade after 8 silent minutes
    s = b.snapshot("C")
    assert s["holes"] == [[M + 2 * MIN_MS, M + 10 * MIN_MS]]
    rows = {r[0]: r for r in s["minutes"]}
    assert rows[M + 10 * MIN_MS][4] == 1 and rows[M + 2 * MIN_MS][4] == 1
    assert rows[M + MIN_MS][4] == 0                         # untouched minutes stay trustworthy


def test_corrupt_file_starts_clean_and_save_failure_is_silent(tmp_path):
    bad = tmp_path / "flow.json"
    bad.write_text("{not json")
    assert FlowStore(path=str(bad)).snapshot("C")["minutes"] == []
    FlowStore(path=str(tmp_path / "nodir" / "x" / "flow.json")).add("C", M, 1, 0)   # creates the dir, never raises
    FlowStore(path="/proc/nope/flow.json", save_every_s=0).add("C", M, 1, 0)         # unwritable: swallowed


def test_summary_window_with_a_hole_is_incomplete():
    from px_flow import summary
    f = FlowStore()
    for i in range(10):
        f.add("C", M + i * MIN_MS + 10, 1, 0)
    f.mark_gap("C", M + 9 * MIN_MS)
    f.add("C", M + 20 * MIN_MS + 10, 1, 0)
    s = summary(f, "C", M + 20 * MIN_MS + 30_000)
    assert s["w"][5]["complete"] is False and s["w"][60]["complete"] is False


def test_hostile_timestamps_are_rejected_and_flag_a_gap():
    f = FlowStore()
    f.add("C", M, 1, 0)
    for bad in (1e20, -1e20, 5, 4_200_000_000_000, float("inf")):
        f.add("C", bad, 1, 0)
    f.add("C", M + MIN_MS, 1, 0)
    s = f.snapshot("C")
    assert s["since"] == M + MIN_MS and all(r[0] >= M for r in s["minutes"])
    assert any(r[4] for r in s["minutes"])                    # the unplaceable trades left a partial flag, not silence


def test_loading_a_hostile_file_never_breaks_the_store(tmp_path):
    import json
    p = tmp_path / "flow.json"
    shapes = [
        {"c": {"C": {"since": "x", "m": {}, "last_ts": 1}}},                                   # string since
        {"c": {"C": {"since": M + MIN_MS, "last_ts": "y", "m": {str(M): [1, 2, 3]}}}},          # string last_ts
        {"c": {"C": {"since": M, "last_ts": M, "m": {str(M): [10**30, 1, 1]}}}},                # absurd volume
        {"c": {f"K{i}": {"since": M, "last_ts": M, "m": {str(M): [1, 1, 0]}} for i in range(50)}},   # contract flood
        {"c": {"C": {"since": M, "last_ts": M, "m": {str(M + i * MIN_MS): [1, 1, 0] for i in range(50_000)}}}},
        [1, 2, 3], "string", None,
    ]
    for raw in shapes:
        p.write_text(json.dumps(raw))
        s = FlowStore(path=str(p))
        assert len(s._c) <= 8 and all(len(c["m"]) <= MAX_MINUTES for c in s._c.values())
        from px_flow import summary
        summary(s, "C", M)                                    # must not raise for any loaded state
        s.snapshot("C")


def test_partial_set_stays_bounded_under_invalid_volume_trades():
    f = FlowStore()
    f.add("C", M, 1, 0)
    for i in range(MAX_MINUTES + 500):
        f.add("C", M + (i + 1) * MIN_MS, 0, 0)                # valid time, invalid volume: no bucket is ever created
    assert len(f._c["C"]["partial"]) <= MAX_MINUTES + 1


def _busy(f, n=65, cid="C"):
    for i in range(n):
        f.add(cid, M + i * MIN_MS + 10, 1, 0)


def test_ongoing_outage_never_ages_into_a_complete_zero_window():
    from px_flow import summary
    f = FlowStore()
    _busy(f)
    f.mark_gap("C", M + 65 * MIN_MS)
    s = summary(f, "C", M + 75 * MIN_MS)
    for w in (5, 15, 60):
        assert s["w"][w]["complete"] is False
    assert f.snapshot("C", M + 75 * MIN_MS)["holes"] == [[M + 64 * MIN_MS, M + 75 * MIN_MS]]


def test_quiet_healthy_tape_stays_complete():
    from px_flow import summary
    f = FlowStore()
    _busy(f)
    s = summary(f, "C", M + 75 * MIN_MS)            # no gap flag: a quiet market, not an outage
    assert s["w"][5] == {"buy": 0, "sell": 0, "unknown": 0, "delta": 0, "complete": True}
    assert f.snapshot("C", M + 75 * MIN_MS)["holes"] == []


def test_open_hole_closes_into_a_normal_hole_when_a_trade_arrives():
    from px_flow import summary
    f = FlowStore()
    _busy(f)
    f.mark_gap("C", M + 65 * MIN_MS)
    f.add("C", M + 70 * MIN_MS + 5, 1, 0)
    assert f.snapshot("C", M + 200 * MIN_MS)["holes"] == [[M + 64 * MIN_MS, M + 70 * MIN_MS]]    # fixed, no longer follows now
    assert summary(f, "C", M + 90 * MIN_MS)["w"][5]["complete"] is True


def test_restart_loaded_store_shows_open_hole_until_first_trade(tmp_path):
    path = str(tmp_path / "flow.json")
    a = FlowStore(path=path, save_every_s=0)
    _busy(a, 3)
    a.save()
    b = FlowStore(path=path)
    assert b.snapshot("C", M + 30 * MIN_MS)["holes"] == [[M + 2 * MIN_MS, M + 30 * MIN_MS]]
    b.add("C", M + 31 * MIN_MS, 1, 0)
    assert b.snapshot("C", M + 40 * MIN_MS)["holes"] == [[M + 2 * MIN_MS, M + 31 * MIN_MS]]


def test_mark_gap_all_contracts_and_unknown_last_ts_has_no_open_hole():
    f = FlowStore()
    f.add("A", M, 1, 0)
    f.add("B", M, 1, 0)
    f.mark_gap()                                      # cid=None flags every contract
    assert f.snapshot("A", M + 5 * MIN_MS)["holes"] == [[M, M + 5 * MIN_MS]]
    assert f.snapshot("B", M + 5 * MIN_MS)["holes"] == [[M, M + 5 * MIN_MS]]
    f._c["B"]["last_ts"] = None
    assert f.snapshot("B", M + 5 * MIN_MS)["holes"] == []
    assert f.snapshot("nope", M)["holes"] == []


def test_odd_now_ms_does_not_raise_and_holes_stay_bounded():
    from px_flow import MAX_HOLES, summary
    f = FlowStore()
    f.add("C", M, 1, 0)
    f.mark_gap("C")
    for bad in (None, float("nan"), float("inf"), "x", True, M - 10 * MIN_MS, 0, -5):
        s = f.snapshot("C", bad)
        assert len(s["holes"]) == 1 and s["holes"][0][0] <= s["holes"][0][1]
        assert summary(f, "C", bad)["w"][5]["complete"] is False
    for i in range(MAX_HOLES + 50):
        f.add("C", M + (i + 1) * 10 * MIN_MS, 1, 0)
        f.mark_gap("C")
    assert len(f.snapshot("C", M)["holes"]) <= MAX_HOLES + 1


def test_a_bad_print_leaves_a_partial_minute_that_makes_its_window_incomplete():       # fuzz pass finding
    from px_flow import summary
    f = FlowStore()
    for i in range(10):
        f.add("C", M + i * MIN_MS, 5, 0)
    f.add("C", M + 10 * MIN_MS, float("nan"), 0)                # the only print in minute 10 is garbage: no bucket
    assert summary(f, "C", M + 10 * MIN_MS)["w"][5]["complete"] is False
    f.add("C", M + 11 * MIN_MS, 5, 0)
    assert summary(f, "C", M + 11 * MIN_MS)["w"][5]["complete"] is False
    f.add("C", M + 12 * MIN_MS, 2.5, 1)                         # a fractional volume is a bad print, never truncated
    assert 2 not in [r[2] for r in f.snapshot("C")["minutes"] if r[0] == M + 12 * MIN_MS and r[4] == 0]


def test_loading_deeply_nested_json_does_not_crash_the_constructor(tmp_path):
    p = tmp_path / "flow.json"
    p.write_bytes(b"[" * 100_000)
    assert FlowStore(path=str(p)).snapshot("C")["minutes"] == []
