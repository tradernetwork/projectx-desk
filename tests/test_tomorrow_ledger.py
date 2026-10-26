"""The Tomorrow's Map record: when a map is filed, that it is filed once, when it is graded, and what the AI surface sees."""
import asyncio
import json
import pathlib
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from projectx_desk import tomorrow as T
from projectx_desk import tomorrow_feed as F
from projectx_desk import tomorrow_grade as G
from projectx_desk import tomorrow_ledger as L
from projectx_desk.tradeday import CT

FXDIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "tomorrow"
MES = "CON.F.US.MES.Z26"


def ct(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CT).timestamp()


def a_map():
    fx = json.loads((FXDIR / "spy-2026-10-17.json").read_text(encoding="utf-8"))
    ladder, share = T.survivor_ladder(fx["matrix"], fx["today"])
    g = fx["gex"]
    return T.build_tomorrow_map(g["spotPrice"], g["gammaFlipLevel"], g["totalGEX"], ladder, T.bars_from_history(fx["historical"]), share, ratio=10.0788, tick=0.25)


def session_bars(d, lo=7600.0, hi=7660.0, n=78):
    """Raw 5m bars, newest first, for the 08:30 to 15:00 CT session of date d."""
    t0 = datetime(d.year, d.month, d.day, 8, 30, tzinfo=CT)
    out = []
    for i in range(n):
        t = (t0 + timedelta(minutes=5 * i)).astimezone(timezone.utc).isoformat()
        c = lo + (hi - lo) * i / max(n - 1, 1)
        out.append({"t": t, "o": c, "h": c + 3, "l": c - 3, "c": c, "v": 1})
    return list(reversed(out))


class Client:
    def __init__(self, rows_by_date=None):
        self.rows_by_date, self.calls = rows_by_date or {}, []

    async def bars(self, params):
        self.calls.append(params)
        d = datetime.fromisoformat(params["startTime"]).astimezone(CT).date()
        return self.rows_by_date.get(d, [])


def fake_app(client=None, tracked=None):
    return types.SimpleNamespace(state=types.SimpleNamespace(tracked=tracked if tracked is not None else {"MES": MES}, client=client or Client()))


@pytest.fixture
def ok_result(monkeypatch):
    calls = []

    async def fake(app, cid, **kw):
        calls.append(cid)
        return {"status": "OK", "root": "MES", "source": "SPY", "ratio": 10.0788, "age_s": 600, "snapshot_epoch": 1, "map": a_map()}
    monkeypatch.setattr(F, "tomorrow_result", fake)
    return calls


def run(c):
    return asyncio.run(c)


def test_next_session_skips_weekends():
    assert [L.next_session(date(2026, 10, d)).isoformat() for d in (23, 24, 25, 26)] == ["2026-10-26", "2026-10-26", "2026-10-26", "2026-10-27"]


def test_a_map_is_filed_only_after_the_cash_close_on_a_weekday_and_only_once(tmp_path, ok_result):
    p, app = tmp_path / "l.jsonl", fake_app()
    run(L.tick(app, p, ct(2026, 10, 26, 15, 9)))                      # Monday, before 15:10
    assert L.load(p) == [] and ok_result == []
    run(L.tick(app, p, ct(2026, 10, 26, 15, 15)))
    run(L.tick(app, p, ct(2026, 10, 26, 16, 0)))                      # the same night again
    rows = L.load(p)
    assert len(rows) == 1 and rows[0]["kind"] == "map" and (rows[0]["root"], rows[0]["session"], rows[0]["made_after"]) == ("MES", "2026-10-27", "2026-10-26")
    assert rows[0]["map_version"] == T.VERSION and rows[0]["map"]["roads"][0]["key"] == "UP" and ok_result == [MES]
    run(L.tick(app, tmp_path / "w.jsonl", ct(2026, 10, 24, 16, 0)))      # Saturday: nothing
    assert L.load(tmp_path / "w.jsonl") == []
    run(L.tick(app, tmp_path / "f.jsonl", ct(2026, 10, 23, 15, 30)))     # Friday night files Monday's session
    assert L.load(tmp_path / "f.jsonl")[0]["session"] == "2026-10-26"


def test_an_old_snapshot_or_a_failed_map_is_never_filed_as_tonights(tmp_path, monkeypatch):
    for res in ({"status": "OK", "root": "MES", "age_s": 7 * 3600, "map": a_map()}, {"status": "UNAVAILABLE", "code": "NO_REF", "map": None},
                {"status": "OK", "root": "MES", "age_s": 10, "map": None}):
        async def fake(app, cid, _r=res, **kw):
            return _r
        monkeypatch.setattr(F, "tomorrow_result", fake)
        p = tmp_path / f"{res['status']}{res.get('age_s')}.jsonl"
        run(L.tick(fake_app(), p, ct(2026, 10, 26, 15, 20)))
        assert L.load(p) == []


def test_a_finished_session_is_graded_once_against_the_futures_session_bar(tmp_path, ok_result):
    p = tmp_path / "l.jsonl"
    client = Client({date(2026, 10, 27): session_bars(date(2026, 10, 27))})
    app = fake_app(client)
    run(L.tick(app, p, ct(2026, 10, 26, 15, 15)))                      # Monday night: map for Tuesday
    run(L.tick(app, p, ct(2026, 10, 27, 15, 4)))                       # Tuesday before 15:05: the session is not over
    assert [r["kind"] for r in L.load(p)] == ["map"] and client.calls == []
    run(L.tick(app, p, ct(2026, 10, 27, 15, 6)))
    run(L.tick(app, p, ct(2026, 10, 27, 15, 30)))                      # no regrade
    rows = L.load(p)
    grades = [r for r in rows if r["kind"] == "grade"]
    assert len(grades) == 1 and grades[0]["grade_version"] == G.GRADE_VERSION and grades[0]["session"] == "2026-10-27"
    bar = grades[0]["bar"]
    assert bar["o"] == pytest.approx(7600.0) and bar["h"] == pytest.approx(7663.0) and bar["l"] == pytest.approx(7597.0) and bar["c"] == pytest.approx(7660.0)
    assert grades[0]["grade"] == G.grade_map(rows[0]["map"], bar) and "took the" in grades[0]["grade"]["summary"] or "whipsawed" in grades[0]["grade"]["summary"]
    assert len(client.calls) == 1 and client.calls[0]["includePartialBar"] is False


def test_an_incomplete_session_is_not_graded_yet_and_an_ancient_one_is_never_tried(tmp_path, ok_result):
    p, client = tmp_path / "l.jsonl", Client({date(2026, 10, 27): session_bars(date(2026, 10, 27), n=40)})
    app = fake_app(client)
    run(L.tick(app, p, ct(2026, 10, 26, 15, 15)))
    run(L.tick(app, p, ct(2026, 10, 27, 16, 0)))                       # only 40 of 78 bars: wait
    assert all(r["kind"] == "map" for r in L.load(p))
    old = [{"kind": "map", "root": "MES", "contract": MES, "session": "2026-10-01", "map_version": T.VERSION, "map": a_map()}]
    assert L.due_grades(old, datetime.fromtimestamp(ct(2026, 10, 27, 16), CT)) == []


def test_load_survives_garbage_and_summarize_counts_only_graded_rows_for_that_root(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text('not json\n[1]\n{"kind":"other"}\n', encoding="utf-8")
    L.append(p, {"kind": "grade", "root": "MES", "session": "2026-10-27", "map_version": T.VERSION, "grade_version": G.GRADE_VERSION,
                 "grade": {"branch": "UP", "hits": 2, "total": 3, "untested": 1, "summary": "s", "null_hits": 1, "null_total": 2}})
    L.append(p, {"kind": "grade", "root": "MNQ", "session": "2026-10-27", "map_version": T.VERSION, "grade_version": G.GRADE_VERSION,
                 "grade": {"branch": "DOWN", "hits": 0, "total": 1, "untested": 0, "summary": "s", "null_hits": 0, "null_total": 1}})
    rows = L.load(p)
    assert len(rows) == 2
    s = L.summarize(rows, "MES")
    assert s["record"]["sessions"] == 1 and (s["record"]["hits"], s["record"]["total"]) == (2, 3) and s["record"]["publishable"] is False
    assert L.summarize([], "MES")["last_call"] is None and L.load(tmp_path / "missing.jsonl") == []


def test_the_digest_says_loading_then_the_map_and_never_a_rate_under_ten_sessions(monkeypatch):
    app = fake_app()
    app.state.tmap_cache = {}

    async def fake(app_, cid, **kw):
        app_.state.tmap_cache[cid] = (__import__("time").monotonic(), {"status": "OK", "root": "MES", "source": "SPY", "ratio": 10.0, "age_s": 1800, "map": a_map()})
        return app_.state.tmap_cache[cid][1]
    monkeypatch.setattr(F, "tomorrow_result", fake)

    async def go():
        first = await F.ai_block(app, MES)
        await app.state.tmap_kick_task
        return first, await F.ai_block(app, MES)
    first, second = run(go())
    assert first == {"status": "UNAVAILABLE", "code": "LOADING"} and second["status"] == "OK" and second["roads"][0]["plain"]
    second["record"] = {"last_call": {"session": "2026-10-27", "branch": "UP", "hits": 1, "total": 2, "untested": 0, "summary": "took the UP road"},
                        "record": {"sessions": 3, "hits": 2, "total": 4, "null_hits": 1, "null_total": 3, "min_sessions": 10, "publishable": False, "map_version": 1}}
    text = "\n".join(F.tomorrow_lines(second))
    assert text.startswith("MAP: SPY->MES age=30min regime=NEGATIVE") and "MAP_UP: IF it goes" in text and "MAP_ELSE: ELSE it holds" in text
    assert "MAP_RECORD: 3 of 10 sessions graded (no rate shown until then)" in text and "levels held" not in text and "MAP_NOTE:" in text
    second["record"]["record"].update(sessions=10, publishable=True)
    assert "10 sessions graded; levels held 2/4 vs round numbers 1/3" in "\n".join(F.tomorrow_lines(second))
    assert F.tomorrow_lines({"status": "UNAVAILABLE", "code": "NO_KEY"}) == ["MAP: NA (NO_KEY)"] and F.tomorrow_lines(None) == []
