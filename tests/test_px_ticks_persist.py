"""Tick bars survive a restart; the restart is a hole, never a bridged bar."""
import json

from projectx_desk import ticks as px_ticks
from projectx_desk.state import LiveState
from projectx_desk.ticks import TickBars, MAX_BARS

T0 = 1_760_000_000_000


def feed(tb, cid, rows):
    for r in rows:
        tb.add(cid, *r)


def make(path, **kw):
    return TickBars(sizes=(3,), path=str(path), save_every_s=1e9, **kw)


def test_round_trip_and_restart_boundary(tmp_path):
    p = tmp_path / "ticks.json"
    tb = make(p)
    feed(tb, "C", [(T0 + i, 10.0 + i, 2, i % 2) for i in range(7)])        # 2 complete bars + 1 print forming
    before = tb.snapshot("C", 3)
    assert [r[9] for r in before["bars"]] == [3, 3, 1]
    tb.save()
    assert not (tmp_path / "ticks.json.tmp").exists()

    tb2 = make(p)
    s = tb2.snapshot("C", 3)
    assert s["since"] == before["since"]
    assert s["bars"][:2] == before["bars"][:2]                              # completed bars are bit-for-bit back
    assert s["bars"][2][8] == 1 and s["bars"][2][9] == 1                    # the bar forming at shutdown is closed SHORT and flagged
    feed(tb2, "C", [(T0 + 10 * 60_000 + i, 50.0, 1, 0) for i in range(4)])  # trading resumes ten minutes later
    rows = tb2.snapshot("C", 3)["bars"]
    assert rows[3][8] == 1 and rows[3][0] > rows[2][0]                      # first bar after the restart is flagged: a hole, not a bridge
    assert rows[4][8] == 0
    assert tb2.snapshot("C", 3)["te"][2] == before["te"][2]                 # the short bar still ends where the old tape ended


def test_corrupt_wrong_version_and_missing_are_ignored(tmp_path, capsys):
    p = tmp_path / "ticks.json"
    assert make(p).snapshot("C", 3)["bars"] == []                           # no file: silent
    assert capsys.readouterr().out == ""
    for body in ("{not json", json.dumps({"v": 2, "c": {}}), json.dumps({"v": 1, "c": {"C": {"since": T0, "done": {"3": [[1, 2]]}}}}),
                 json.dumps([1, 2, 3]), json.dumps({"v": 1, "c": {"C": {"since": "x"}}})):
        p.write_text(body)
        tb = make(p)
        assert tb.snapshot("C", 3)["bars"] == []
        out = capsys.readouterr().out
        assert out.count("ticks: ignoring") == 1, body                      # exactly one warning, no crash
    tb.add("C", T0, 10.0, 1, 0)                                             # and it is usable afterwards
    assert tb.snapshot("C", 3)["since"] == T0


def test_nonfinite_and_out_of_order_rows_rejected(tmp_path, capsys):
    p = tmp_path / "ticks.json"
    good = [T0, T0 + 1, 1.0, 1.0, 1.0, 1.0, 3, 1, 2, 0, 3, 0]
    bad_px = list(good); bad_px[2] = float("nan")
    for rows in ([good, [T0 - 5] + good[1:]], [bad_px]):
        p.write_text(json.dumps({"v": 1, "c": {"C": {"since": T0, "done": {"3": rows}}}}))
        assert make(p).snapshot("C", 3)["bars"] == []
    assert capsys.readouterr().out.count("ticks: ignoring") == 2


def test_cap_per_contract_and_size(tmp_path):
    p = tmp_path / "ticks.json"
    tb = TickBars(sizes=(1,), path=str(p), save_every_s=1e9)
    feed(tb, "C", [(T0 + i, 10.0, 1, 0) for i in range(MAX_BARS + 120)])
    tb.save()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert len(raw["c"]["C"]["done"]["1"]) == MAX_BARS == 5000
    tb2 = TickBars(sizes=(1,), path=str(p))
    assert len(tb2._c["C"]["done"][1]) == MAX_BARS
    assert tb2.snapshot("C", 1, limit=10**6)["bars"][-1][0] == T0 + MAX_BARS + 119


def test_periodic_save_and_state_wiring(tmp_path):
    p = tmp_path / "ticks.json"
    ls = LiveState(None, None, ticks_path=str(p))
    ls.ticks.sizes = (3,)
    ls.ticks._every = 0.0
    ls._on_trade("C", [{"timestamp": T0 + i, "price": 5.0, "volume": 1, "type": 0} for i in range(5)])
    assert p.exists() and json.loads(p.read_text(encoding="utf-8"))["v"] == 1               # written while prints flow, no explicit save
    assert LiveState(None, None).ticks._path is None                        # no path = in-memory only


def test_unwritable_path_never_stops_the_tape(tmp_path):
    tb = TickBars(sizes=(3,), path=str(tmp_path / "nodir" / "x" / "ticks.json"), save_every_s=0.0)
    (tmp_path / "nodir").write_text("a file where a directory should be")
    feed(tb, "C", [(T0 + i, 1.0, 1, 0) for i in range(4)])
    assert len(tb.snapshot("C", 3)["bars"]) == 2


def test_no_bare_write_attribute_in_new_code():
    import pathlib
    from test_px_guard_pin import violations
    root = pathlib.Path(px_ticks.__file__).parent
    for name in ("ticks.py", "recorder.py", "app.py", "state.py"):
        assert violations((root / name).read_text(encoding="utf-8"), f"projectx_desk/{name}") == [], name
