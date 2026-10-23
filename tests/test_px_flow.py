import json
import sqlite3
import time

from projectx_desk.flow import MAX_MINUTES, MIN_MS, SERVE_MINUTES, FlowStore, summary

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
    from projectx_desk.state import LiveState
    live = LiveState()
    live._on_quote("C", {"bestBid": 10.0, "bestAsk": 10.25, "lastPrice": 10.0})
    live._on_quote("C", {"bestAsk": 10.5})                    # ask-only update
    q = live.quotes["C"]
    assert (q["bestBid"], q["bestAsk"], q["lastPrice"]) == (10.0, 10.5, 10.0)
    live._on_quote("C", {"bestBid": None, "bestAsk": 10.75})   # an explicit null never erases a known value
    assert live.quotes["C"]["bestBid"] == 10.0


def test_restart_keeps_buckets_and_turns_downtime_into_a_hole(tmp_path):
    f = str(tmp_path / "flow.db")
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


def test_summary_window_with_a_hole_is_incomplete():
    from projectx_desk.flow import summary
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
    from projectx_desk.flow import summary
    f = FlowStore()
    _busy(f)
    f.mark_gap("C", M + 65 * MIN_MS)
    s = summary(f, "C", M + 75 * MIN_MS)
    for w in (5, 15, 60):
        assert s["w"][w]["complete"] is False
    assert f.snapshot("C", M + 75 * MIN_MS)["holes"] == [[M + 64 * MIN_MS, M + 75 * MIN_MS]]


def test_quiet_healthy_tape_stays_complete():
    from projectx_desk.flow import summary
    f = FlowStore()
    _busy(f)
    s = summary(f, "C", M + 75 * MIN_MS)            # no gap flag: a quiet market, not an outage
    assert s["w"][5] == {"buy": 0, "sell": 0, "unknown": 0, "delta": 0, "complete": True}
    assert f.snapshot("C", M + 75 * MIN_MS)["holes"] == []


def test_open_hole_closes_into_a_normal_hole_when_a_trade_arrives():
    from projectx_desk.flow import summary
    f = FlowStore()
    _busy(f)
    f.mark_gap("C", M + 65 * MIN_MS)
    f.add("C", M + 70 * MIN_MS + 5, 1, 0)
    assert f.snapshot("C", M + 200 * MIN_MS)["holes"] == [[M + 64 * MIN_MS, M + 70 * MIN_MS]]    # fixed, no longer follows now
    assert summary(f, "C", M + 90 * MIN_MS)["w"][5]["complete"] is True


def test_restart_loaded_store_shows_open_hole_until_first_trade(tmp_path):
    path = str(tmp_path / "flow.db")
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
    from projectx_desk.flow import MAX_HOLES, summary
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
    from projectx_desk.flow import summary
    f = FlowStore()
    for i in range(10):
        f.add("C", M + i * MIN_MS, 5, 0)
    f.add("C", M + 10 * MIN_MS, float("nan"), 0)                # the only print in minute 10 is garbage: no bucket
    assert summary(f, "C", M + 10 * MIN_MS)["w"][5]["complete"] is False
    f.add("C", M + 11 * MIN_MS, 5, 0)
    assert summary(f, "C", M + 11 * MIN_MS)["w"][5]["complete"] is False
    f.add("C", M + 12 * MIN_MS, 2.5, 1)                         # a fractional volume is a bad print, never truncated
    assert 2 not in [r[2] for r in f.snapshot("C")["minutes"] if r[0] == M + 12 * MIN_MS and r[4] == 0]


def test_a_reconnect_that_resumes_within_15_seconds_does_not_blank_the_bar():
    from projectx_desk.flow import SHORT_GAP_MS, summary
    f = FlowStore()
    for i in range(10):
        f.add("C", M + i * MIN_MS + 1_000, 5, 0)
    f.mark_gap("C", M + 9 * MIN_MS + 2_000)
    f.add("C", M + 9 * MIN_MS + 1_000 + SHORT_GAP_MS, 5, 1)           # trades resume exactly at the tolerance: a blip
    s = f.snapshot("C", M + 9 * MIN_MS + 30_000)
    assert s["holes"] == [] and all(r[4] == 0 for r in s["minutes"][1:])
    assert summary(f, "C", M + 9 * MIN_MS + 30_000)["w"][5]["complete"] is True


def test_a_longer_outage_is_still_flagged_and_an_ongoing_blip_shows_no_open_hole():
    f = FlowStore()
    for i in range(5):
        f.add("C", M + i * MIN_MS + 1_000, 5, 0)
    f.mark_gap("C", M + 4 * MIN_MS + 2_000)
    assert f.snapshot("C", M + 4 * MIN_MS + 1_000 + 10_000)["holes"] == []           # 10 s into an outage: still a blip, no flicker
    assert f.snapshot("C", M + 4 * MIN_MS + 1_000 + 20_000)["holes"] != []           # 20 s in: now it is an open hole
    f.add("C", M + 4 * MIN_MS + 1_000 + 20_000, 5, 1)                                # resumes after 20 s: that minute is partial
    assert {r[0]: r[4] for r in f.snapshot("C", M + 6 * MIN_MS)["minutes"]}[M + 4 * MIN_MS] == 1


# ---- SQLite persistence ---------------------------------------------------------------------------------------

def test_unreadable_database_is_moved_aside_and_an_unwritable_path_never_raises(tmp_path):
    bad = tmp_path / "flow.db"
    bad.write_text("{not a database")
    s = FlowStore(path=str(bad))
    assert s.snapshot("C")["minutes"] == []
    s.add("C", M, 1, 0)
    s.save()
    assert list(tmp_path.glob("flow.db.corrupt-*")), "the unreadable file is kept, not deleted"
    assert FlowStore(path=str(bad)).snapshot("C")["minutes"][0][0] == M
    FlowStore(path=str(tmp_path / "nodir" / "x" / "flow.db")).add("C", M, 1, 0)       # creates the dir, never raises
    FlowStore(path="/proc/nope/flow.db", save_every_s=0).add("C", M, 1, 0)             # unwritable: memory only, swallowed


def test_history_beyond_the_in_memory_window_survives_and_is_served(tmp_path):
    path = str(tmp_path / "flow.db")
    a = FlowStore(path=path, save_every_s=1e9)
    n = MAX_MINUTES + 500
    for i in range(n):
        a.add("C", M + i * MIN_MS + 5, 3, 0)
    a.save()
    assert len(a._c["C"]["m"]) == MAX_MINUTES                         # memory stays bounded
    snap = a.snapshot("C")
    assert len(snap["minutes"]) == n and snap["since"] == M + 0 * MIN_MS + MIN_MS      # nothing was dropped, coverage did not shrink
    assert [r[0] for r in snap["minutes"]] == [M + i * MIN_MS for i in range(n)]
    b = FlowStore(path=path)                                           # restart: the tail loads, the rest is read on demand
    assert len(b._c["C"]["m"]) == MAX_MINUTES
    assert len(b.snapshot("C")["minutes"]) == n


def test_snapshot_after_returns_only_the_tail_and_never_more_than_serve_minutes(tmp_path):
    a = FlowStore(path=str(tmp_path / "flow.db"), save_every_s=1e9)
    for i in range(20):
        a.add("C", M + i * MIN_MS + 5, 1, 0)
    tail = a.snapshot("C", after=M + 15 * MIN_MS)["minutes"]
    assert [r[0] for r in tail] == [M + i * MIN_MS for i in range(15, 20)]
    for bad in (-1, True, "x", 1.5):
        assert len(a.snapshot("C", after=bad)["minutes"]) == 20          # a nonsense bound is ignored, not trusted
    assert SERVE_MINUTES >= MAX_MINUTES


def test_unsaved_minute_evicted_from_memory_is_still_written(tmp_path):
    path = str(tmp_path / "flow.db")
    a = FlowStore(path=path, save_every_s=1e9)
    for i in range(MAX_MINUTES + 3):
        a.add("C", M + i * MIN_MS + 5, 2, 0)                          # nothing flushed yet: the first minutes are evicted dirty
    a.save()
    con = sqlite3.connect(path)
    got = con.execute("SELECT buy FROM minutes WHERE cid='C' AND t=?", (M,)).fetchone()
    assert got == (2,)


def test_holes_and_partials_persist_and_load_with_the_tail(tmp_path):
    path = str(tmp_path / "flow.db")
    a = FlowStore(path=path, save_every_s=1e9)
    a.add("C", M + 5, 1, 0)
    a.mark_gap("C")
    a.add("C", M + 10 * MIN_MS + 5, 1, 1)                              # resumes after a real outage: hole [M, M+10m]
    a.save()
    b = FlowStore(path=path)
    assert [M, M + 10 * MIN_MS] in b.snapshot("C")["holes"]
    rows = {r[0]: r for r in b.snapshot("C")["minutes"]}
    assert rows[M][4] == 1 and rows[M + 10 * MIN_MS][4] == 1


def test_retention_prunes_old_minutes_and_zero_keeps_everything(tmp_path):
    now = int(time.time() * 1000) // MIN_MS * MIN_MS
    old = now - 100 * 86_400_000
    for keep, want_old in ((90, False), (0, True)):
        path = str(tmp_path / f"flow{keep}.db")
        a = FlowStore(path=path, save_every_s=1e9, keep_days=keep)
        a.add("C", old + 5, 1, 0)
        a.add("C", now + 5, 1, 0)
        a.save()
        b = FlowStore(path=path, keep_days=keep)
        ts = [r[0] for r in b.snapshot("C", after=None)["minutes"]]
        assert (old in ts) is want_old and now in ts


def test_legacy_flow_json_is_imported_once_then_renamed(tmp_path):
    legacy = tmp_path / "flow.json"
    legacy.write_text(json.dumps({"v": 1, "c": {"C": {"since": M, "last_ts": M + 2 * MIN_MS, "partial": [M + 2 * MIN_MS],
                                                       "holes": [[M + MIN_MS, M + 2 * MIN_MS]],
                                                       "m": {str(M): [4, 5, 0], str(M + MIN_MS): [1, 1, 0]}}}}))
    db = str(tmp_path / "flow.db")
    a = FlowStore(path=db, legacy_json=str(legacy))
    assert [r[:4] for r in a.snapshot("C")["minutes"] if r[0] in (M, M + MIN_MS)] == [[M, 4, 5, 0], [M + MIN_MS, 1, 1, 0]]
    assert not legacy.exists() and (tmp_path / "flow.json.migrated").exists()
    b = FlowStore(path=db, legacy_json=str(legacy))                    # a second start finds the data in the database
    assert len(b.snapshot("C")["minutes"]) >= 2


def test_hostile_rows_in_the_database_never_break_the_store(tmp_path):
    path = str(tmp_path / "flow.db")
    FlowStore(path=path).save()
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO meta VALUES (?,?,?)", [("A", "x", 1), ("B", M, "y"), ("C", M, M), ("D", M, M)] + [(f"K{i}", M, M) for i in range(50)])
    con.executemany("INSERT INTO minutes VALUES (?,?,?,?,?,?)", [
        ("C", M, 2**62, 1, 1, 0),                  # absurd volume
        ("C", M + MIN_MS, "z", 1, 1, 0),          # text in an integer column
        ("C", M + 2 * MIN_MS, 1, 1, 0, 0),        # the one good row
        ("D", 5, 1, 1, 1, 0),                      # timestamp outside 2000-2100
    ])
    con.execute("INSERT INTO holes VALUES ('C', 'q', 7)")
    con.commit()
    con.close()
    s = FlowStore(path=path)
    assert len(s._c) <= 8
    assert [r[0] for r in s.snapshot("C")["minutes"]] == [M + 2 * MIN_MS]
    summary(s, "C", M)                              # must not raise for any loaded state
