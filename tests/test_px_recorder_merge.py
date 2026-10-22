"""Recorder history import: pure merge, sanitizing, config, polling. The recorder is untrusted and may be down."""
import asyncio
import json
import math

import pytest

from projectx_desk import recorder as R
from projectx_desk.recorder import RecorderCache, RecorderError, clean_flow, clean_ticks, merge_flow, merge_ticks

M = 60_000
B = (1_760_000_000_000 // M) * M          # a minute boundary
ASOF = B + 100 * M + 30_000               # the recorder was read 100.5 minutes in


def snap(since, rows, holes=()):
    return {"v": 1, "since": since, "minutes": [list(r) for r in rows], "holes": [list(h) for h in holes]}


def by_minute(s):
    return {r[0]: r for r in s["minutes"]}


# ---- flow merge -------------------------------------------------------------------------------

def test_local_complete_wins_and_nothing_is_summed():
    local = snap(B, [[B + 5 * M, 10, 20, 0, 0]])
    remote = snap(B, [[B + 5 * M, 999, 999, 0, 0], [B + 6 * M, 7, 8, 1, 0]])
    out, used = merge_flow(local, remote, ASOF)
    rows = by_minute(out)
    assert rows[B + 5 * M] == [B + 5 * M, 10, 20, 0, 0]                    # local bucket, untouched, not 10+999
    assert rows[B + 6 * M] == [B + 6 * M, 7, 8, 1, 0] and used


def test_recorder_fills_a_local_hole_and_the_hole_shrinks():
    hole = [B + 10 * M, B + 20 * M]
    local = snap(B, [[B + 10 * M, 1, 1, 0, 1], [B + 20 * M, 2, 2, 0, 1], [B + 21 * M, 3, 3, 0, 0]], [hole])
    remote = snap(B, [[B + 10 * M, 5, 6, 0, 0], [B + 12 * M, 9, 9, 0, 0], [B + 20 * M, 4, 4, 0, 0]])
    out, used = merge_flow(local, remote, ASOF)
    rows = by_minute(out)
    assert rows[B + 10 * M] == [B + 10 * M, 5, 6, 0, 0] and rows[B + 12 * M][1] == 9 and rows[B + 20 * M][1] == 4
    assert rows[B + 21 * M][1] == 3                                         # local complete minute beside the hole stays local
    assert out["holes"] == [] and used


def test_partial_on_both_sides_stays_a_hole():
    local = snap(B, [[B + 3 * M, 1, 1, 0, 1]])
    remote = snap(B, [[B + 3 * M, 8, 8, 0, 1], [B + 4 * M, 5, 5, 0, 1]])
    out, _ = merge_flow(local, remote, ASOF)
    rows = by_minute(out)
    assert rows[B + 3 * M] == [B + 3 * M, 1, 1, 0, 1] and B + 4 * M not in rows      # no remote numbers leak into a partial minute
    local2 = snap(B + 9 * M, [])                                                      # local was blind at B+4M: a marked hole, still no numbers
    out, _ = merge_flow(local2, remote, ASOF)
    assert by_minute(out)[B + 4 * M] == [B + 4 * M, 0, 0, 0, 1]


def test_both_in_holes_stays_a_hole_and_rows_are_not_invented():
    h = [B + 30 * M, B + 40 * M]
    local = snap(B, [[B + 30 * M, 1, 1, 0, 1]], [h])
    remote = snap(B, [[B + 30 * M, 2, 2, 0, 0]], [[B + 28 * M, B + 45 * M]])
    out, used = merge_flow(local, remote, ASOF)
    assert by_minute(out)[B + 30 * M][1:] == [1, 1, 0, 1]
    assert out["holes"] == [h]


def test_recorder_forming_minute_and_after_are_not_used_and_open_local_hole_survives():
    k = ASOF // M * M
    local = snap(B, [], [[k - 5 * M, k + 10 * M]])                          # local outage that is still open past the read time
    remote = snap(B, [[k - 3 * M, 4, 4, 0, 0], [k, 99, 99, 0, 0]])
    out, _ = merge_flow(local, remote, ASOF)
    rows = by_minute(out)
    assert rows[k - 3 * M][1] == 4 and rows[k] == [k, 0, 0, 0, 1]      # the recorder's forming minute is a marked hole, its 99 is never used
    assert out["holes"] == [[k, k + 10 * M]]                                # only the part the recorder never saw


def test_since_is_the_earliest_honest_start_and_pre_since_is_not_a_hole():
    local = snap(B + 50 * M, [[B + 51 * M, 1, 1, 0, 0]])
    remote = snap(B + 10 * M, [[B + 11 * M, 2, 2, 0, 0]])
    out, used = merge_flow(local, remote, ASOF)
    assert out["since"] == B + 10 * M and out["holes"] == [] and used
    assert by_minute(out)[B + 11 * M][1] == 2


def test_empty_local_takes_the_recorder_history():
    out, used = merge_flow(snap(None, []), snap(B, [[B + M, 1, 2, 0, 0]], [[B + 5 * M, B + 6 * M]]), ASOF)
    assert used and out["since"] == B and out["holes"] == [[B + 5 * M, B + 6 * M]]
    assert by_minute(out)[B + M][1:3] == [1, 2]


def test_no_remote_or_bad_asof_changes_nothing():
    local = snap(B, [[B + M, 1, 1, 0, 0]])
    assert merge_flow(local, None, ASOF) == (local, False)
    assert merge_flow(local, snap(B, []), float("nan")) == (local, False)
    out, used = merge_flow(local, snap(B, []), ASOF)
    assert not used and out == local


# ---- sanitizing -------------------------------------------------------------------------------

def test_clean_flow_accepts_a_good_body_and_marks_bad_rows_partial():
    raw = {"v": 1, "since": B, "minutes": [[B, 1, 2, 3, 0], [B + M, "x", 2, 3, 0], [B + 2 * M, 1.5, 2, 3, 0], [B + 3 * M, -1, 0, 0, 0],
                                           [B + 4 * M, 1, 2, 3, True], [B + 5 * M, 1, 2]], "holes": [[B, B + M]]}
    c = clean_flow(raw)
    rows = by_minute(c)
    assert rows[B] == [B, 1, 2, 3, 0]
    assert all(rows[B + i * M] == [B + i * M, 0, 0, 0, 1] for i in range(1, 6))        # malformed => a partial hole, never a quiet zero
    assert c["holes"] == [[B, B + M]]


@pytest.mark.parametrize("raw", [None, [], {"v": 2}, {"v": 1, "minutes": {}, "holes": []}, {"v": 1, "minutes": [], "holes": [], "since": B + 1},
                                 {"v": 1, "minutes": [["x", 1, 1, 1, 0]], "holes": []}, {"v": 1, "minutes": [[B, 1, 1, 1, 0], [B, 2, 2, 2, 0]], "holes": []},
                                 {"v": 1, "minutes": [], "holes": [[B + M, B]]}, {"v": 1, "minutes": [], "holes": [["a", B]]},
                                 {"v": 1, "minutes": [[float("nan"), 1, 1, 1, 0]], "holes": []}])
def test_clean_flow_rejects_garbage(raw):
    with pytest.raises(RecorderError) as e:
        clean_flow(raw)
    assert e.value.code == "BAD_DATA"


def test_clean_flow_oversize():
    with pytest.raises(RecorderError) as e:
        clean_flow({"v": 1, "since": B, "minutes": [[B + i * M, 1, 1, 0, 0] for i in range(R.MAX_FLOW_ROWS + 1)], "holes": []})
    assert e.value.code == "OVERSIZE"
    with pytest.raises(RecorderError) as e:
        clean_flow({"v": 1, "since": B, "minutes": [], "holes": [[B, B]] * (R.MAX_HOLES + 1)})
    assert e.value.code == "OVERSIZE"


# ---- ticks ------------------------------------------------------------------------------------

def bar(t, n=3, gap=0, px=10.0):
    return [t, px, px + 1, px - 1, px, 3, 2, 1, gap, n]


def tsnap(rows, te, since=None):
    return {"v": 1, "n": 3, "since": since if since is not None else (rows[0][0] if rows else None), "bars": rows, "te": te}


def test_clean_ticks_drops_bad_bars_only():
    inf = float("inf")
    raw = {"v": 1, "n": 3, "since": B, "bars": [bar(B), bar(B + 10, px=inf), [B + 20, 1, 2], bar(B + 30, n=0), bar(B + 40, gap=2), bar(B + 50)],
           "te": [B + 5, B + 15, B + 25, B + 35, B + 45, B + 55]}
    c = clean_ticks(raw, 3)
    assert [r[0] for r in c["bars"]] == [B, B + 50] and c["te"] == [B + 5, B + 55]


@pytest.mark.parametrize("raw", [None, {"v": 1, "n": 4, "bars": [], "te": []}, {"v": 1, "n": 3, "bars": [], "te": [1]}, {"v": 1, "n": 3, "bars": {}, "te": []}])
def test_clean_ticks_rejects_garbage(raw):
    with pytest.raises(RecorderError):
        clean_ticks(raw, 3)


def test_clean_ticks_oversize():
    with pytest.raises(RecorderError) as e:
        clean_ticks({"v": 1, "n": 3, "since": B, "bars": [bar(B + i) for i in range(R.px_ticks.MAX_BARS + 2)], "te": [B + i for i in range(R.px_ticks.MAX_BARS + 2)]}, 3)
    assert e.value.code == "OVERSIZE"


def test_merge_ticks_fills_only_time_the_local_desk_did_not_record():
    local = tsnap([bar(B + 1000, n=3), bar(B + 5000, n=1, gap=1)], [B + 1500, B + 5200])
    remote = {"since": B, "bars": [bar(B), bar(B + 900), bar(B + 1400), bar(B + 3000), bar(B + 3500, gap=1), bar(B + 4000, n=2), bar(B + 9000)],
              "te": [B + 100, B + 950, B + 1450, B + 3100, B + 3600, B + 4100, B + 9100]}
    out, used = merge_ticks(local, remote, 3)
    ts = [r[0] for r in out["bars"]]
    assert ts == [B, B + 900, B + 1000, B + 3000, B + 5000, B + 9000] and used      # B+900 ends at 950, before the local bar starts
    assert B + 1400 not in ts                                                # overlaps the local bar [1000, 1500]
    assert B + 3500 not in ts and B + 4000 not in ts                         # gap-flagged and short bars never imported
    assert out["since"] == B and len(out["te"]) == len(out["bars"])
    assert [r for r in out["bars"] if r[0] in (B + 1000, B + 5000)] == local["bars"]    # the local bars are unchanged


def test_merge_ticks_local_restart_gap_is_filled_by_recorder_but_flagged_bars_stay():
    # local: bar ended at 1500 (closed short after restart, gap=1), next local bar starts at 90_000
    local = tsnap([bar(B + 1000, n=2, gap=1), bar(B + 90_000, gap=1)], [B + 1500, B + 90_500])
    remote = {"since": B, "bars": [bar(B + 20_000), bar(B + 40_000)], "te": [B + 30_000, B + 50_000]}
    out, _ = merge_ticks(local, remote, 3)
    assert [r[0] for r in out["bars"]] == [B + 1000, B + 20_000, B + 40_000, B + 90_000]
    assert out["bars"][0][8] == 1 and out["bars"][3][8] == 1


def test_merge_ticks_noop_cases():
    local = tsnap([bar(B)], [B + 5])
    assert merge_ticks(local, None, 3) == (local, False)
    assert merge_ticks(local, {"since": B, "bars": [], "te": []}, 3) == (local, False)
    assert merge_ticks({"v": 1, "n": 3, "since": B, "bars": [bar(B)]}, {"since": B, "bars": [bar(B - 9000)], "te": [B - 8000]}, 3)[1] is False   # no spans: cannot prove no overlap
    out, used = merge_ticks({"v": 1, "n": 3, "since": None, "bars": []}, {"since": B, "bars": [bar(B)], "te": [B + 5]}, 3)
    assert used and out["since"] == B and len(out["bars"]) == 1


# ---- config, cache, polling -------------------------------------------------------------------

def test_recorder_config_trust_rules():
    assert R.recorder_config({}) is None
    assert R.recorder_config({"PX_RECORDER_URL": "https://rec.lan:8601/", "PX_RECORDER_TOKEN": " t "}) == ("https://rec.lan:8601", "t")
    assert R.recorder_config({"PX_RECORDER_URL": "http://127.0.0.1:18601"}) == ("http://127.0.0.1:18601", "")
    for bad in ("http://rec.lan:8601", "ftp://x", "https://u:p@rec.lan", "https://rec.lan/?a=1", "https://", "https://rec.lan:99999", "nonsense"):
        with pytest.raises(RecorderError) as e:
            R.recorder_config({"PX_RECORDER_URL": bad, "PX_RECORDER_TOKEN": "secret"})
        assert e.value.code == "CONFIG" and "secret" not in str(e.value)


def run(coro):
    return asyncio.run(coro)


def test_recorder_down_changes_nothing_and_says_why():
    async def down(*a, **k):
        raise RecorderError("TIMEOUT")

    async def boom(*a, **k):
        raise RuntimeError("secret remote text token=abc")
    for fetch, code in ((down, "TIMEOUT"), (boom, "UPSTREAM_ERROR")):
        cache = RecorderCache()
        run(R.poll_once(cache, ("https://rec.lan", "tok"), ["CON.F.US.MES.Z25"], (1024,), fetch))
        assert cache.status == f"unavailable ({code})" and "abc" not in cache.status and "tok" not in cache.status
        local = snap(B, [[B + M, 1, 1, 0, 0]])
        assert cache.merge_flow("CON.F.US.MES.Z25", local) == (local, False)
        assert cache.merge_ticks("CON.F.US.MES.Z25", 1024, tsnap([bar(B)], [B + 5])) == (tsnap([bar(B)], [B + 5]), False)


def test_garbage_from_the_recorder_is_ignored():
    async def junk(base, path, params, token, max_body):
        return {"v": 1, "minutes": "lol", "holes": 5} if path == "/api/flow" else {"nope": 1}
    cache = RecorderCache()
    run(R.poll_once(cache, ("https://rec.lan", ""), ["C"], (1024,), junk))
    assert cache.status == "unavailable (BAD_DATA)" and not cache.flow and not cache.ticks


def test_poll_ok_caches_sanitized_data_and_merge_uses_it():
    seen = []

    async def good(base, path, params, token, max_body):
        seen.append((path, dict(params), token))
        if path == "/api/flow":
            return {"v": 1, "since": B, "minutes": [[B + M, 4, 5, 0, 0]], "holes": []}
        return {"v": 1, "n": params["n"], "since": B, "bars": [bar(B)], "te": [B + 5]}
    cache = RecorderCache()
    run(R.poll_once(cache, ("https://rec.lan", "tok"), ["C"], (3,), good))
    assert cache.status == "ok" and [s[0] for s in seen] == ["/api/flow", "/api/tickbars"] and seen[1][1]["n"] == 3
    out, used = cache.merge_flow("C", snap(None, []))
    assert used and by_minute(out)[B + M][1:3] == [4, 5]
    out, used = cache.merge_ticks("C", 3, {"v": 1, "n": 3, "since": None, "bars": []})
    assert used and len(out["bars"]) == 1
    # expiry: a recorder that stopped answering stops contributing
    assert cache.merge_flow("C", snap(None, []), now=10**9)[1] is False


def test_fetch_json_error_mapping_and_body_cap():
    import httpx

    def mk(handler):
        real = httpx.AsyncClient
        return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    cases = [(lambda r: httpx.Response(401), "AUTH"), (lambda r: httpx.Response(500, text="secret"), "UPSTREAM_ERROR"),
             (lambda r: httpx.Response(200, content=b"x" * 50), "OVERSIZE"), (lambda r: httpx.Response(200, content=b"NaN"), "BAD_DATA"),
             (lambda r: httpx.Response(200, content=b"not json"), "BAD_DATA")]
    for handler, code in cases:
        orig = R.httpx.AsyncClient
        R.httpx.AsyncClient = mk(handler)
        try:
            with pytest.raises(RecorderError) as e:
                run(R.fetch_json("https://rec.lan", "/api/flow", {"contract": "C"}, "tok", 40))
            assert e.value.code == code and "secret" not in str(e.value)
        finally:
            R.httpx.AsyncClient = orig

    def ok(request):
        assert request.headers["authorization"].startswith("Basic ") and "tok" not in str(request.url)
        return httpx.Response(200, json={"v": 1})
    orig = R.httpx.AsyncClient
    R.httpx.AsyncClient = mk(ok)
    try:
        assert run(R.fetch_json("https://rec.lan", "/api/flow", {"contract": "C"}, "tok", 1000)) == {"v": 1}
    finally:
        R.httpx.AsyncClient = orig


# ---- wired into the trading desk's routes -----------------------------------------------------

def _desk(monkeypatch, url):
    from fastapi.testclient import TestClient
    from projectx_desk.app import create_app
    from projectx_desk.hub import SignalRHub

    class Fake:
        async def token(self, fresh=False):
            return "jwt"

        async def aclose(self):
            pass
    monkeypatch.delenv("PX_RECORD_ONLY", raising=False)
    monkeypatch.delenv("PX_RECORDER_URL", raising=False)
    if url:
        monkeypatch.setenv("PX_RECORDER_URL", url)
    f = Fake()
    app = create_app(client=f, market_hub=SignalRHub(f, "market", url="ws://127.0.0.1:1/x"), user_hub=SignalRHub(f, "user", url="ws://127.0.0.1:1/y"),
                     allowed_hosts={"testserver"}, connect=False)
    return app, TestClient(app)


def test_desk_routes_off_by_default_have_source_local(monkeypatch):
    app, tc = _desk(monkeypatch, None)
    with tc as c:
        f = c.get("/api/flow", params={"contract": "CON.F.US.MES.Z25"}).json()
        assert f["source"] == "local" and "recorder" not in f
        assert c.get("/state.json").json()["feed"]["recorder"] == "off"


def test_desk_routes_merge_when_a_recorder_answered(monkeypatch):
    cid = "CON.F.US.MES.Z25"
    app, tc = _desk(monkeypatch, "https://rec.lan:8601")
    with tc as c:
        rc = app.state.recorder
        first = (B // M) * M
        rc.put_flow(cid, snap(first, [[first + M, 4, 5, 0, 0]]), first + 50 * M)
        rc.put_ticks(cid, 1024, {"since": B, "bars": [bar(B, n=1024)], "te": [B + 5]})
        rc.status = "ok"
        f = c.get("/api/flow", params={"contract": cid}).json()
        assert f["source"] == "merged" and f["recorder"] == "ok" and f["minutes"][0][:4] == [first + M, 4, 5, 0]
        t = c.get("/api/tickbars", params={"contract": cid, "n": 1024, "limit": 10}).json()
        assert t["source"] == "merged" and len(t["bars"]) == 1
        # shape the UI reads is intact
        assert set(f) >= {"v", "since", "minutes", "holes"} and set(t) >= {"v", "n", "since", "bars"}
        rc.status = "unavailable (TIMEOUT)"
        assert c.get("/state.json").json()["feed"]["recorder"] == "unavailable (TIMEOUT)"


def test_bad_recorder_url_surfaces_config_and_never_polls(monkeypatch):
    app, tc = _desk(monkeypatch, "http://rec.lan:8601")
    with tc as c:
        assert app.state.recorder.status == "unavailable (CONFIG)"
        assert c.get("/api/flow", params={"contract": "CON.F.US.MES.Z25"}).json()["source"] == "local"
