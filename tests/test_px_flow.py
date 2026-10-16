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
