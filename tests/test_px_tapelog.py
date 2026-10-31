import ast
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

from projectx_desk import tapelog
from projectx_desk.state import LiveState

ROOT = Path(__file__).resolve().parent.parent
MES = "CON.F.US.MES.Z26"
# 2026-10-28 15:00:00 UTC = 10:00 CT Wednesday, trading day 2026-10-28
T0 = int(datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)


def names(d):
    return sorted(n for n in os.listdir(d) if n != "px_runtime")


def lines(d, name):
    return (Path(d) / name).read_text().splitlines()


def test_default_off_and_needs_roots(monkeypatch, tmp_path):
    for k in ("PX_TAPE_LOG", "PX_TAPE_ROOTS"):
        monkeypatch.delenv(k, raising=False)
    assert tapelog.from_env(tmp_path) is None
    monkeypatch.setenv("PX_TAPE_LOG", "1")
    assert tapelog.from_env(tmp_path) is None            # no roots named: nothing logged
    monkeypatch.setenv("PX_TAPE_ROOTS", "mes, MNQ")
    t = tapelog.from_env(tmp_path)
    assert t.roots == {"MES", "MNQ"}
    monkeypatch.setenv("PX_TAPE_LOG", "true")
    assert tapelog.from_env(tmp_path) is None
    monkeypatch.setenv("PX_TAPE_LOG", "1")
    monkeypatch.setenv("PX_TAPE_ROOTS", "*")
    assert tapelog.from_env(tmp_path).roots is None
    monkeypatch.setenv("PX_TAPE_KEEP_DAYS", "junk")
    assert tapelog.from_env(tmp_path).keep_days == 30


def test_lines_sides_and_roots(tmp_path):
    t = tapelog.TapeLog(str(tmp_path), {"MES"})
    t.add(MES, T0, 7000.25, 3, 0)
    t.add(MES, T0 + 1, 7000.0, 1, 1)
    t.add(MES, T0 + 2, 7000.0, 2, None)
    t.add("CON.F.US.MNQ.Z26", T0, 1.0, 1, 0)             # root not selected
    t.add(MES, T0, float("nan"), 1, 0)                   # bad number
    t.add("junk", T0, 1.0, 1, 0)
    t.close()
    assert lines(tmp_path, "MES_2026-10-28.tape") == [f"{T0},7000.25,3,B,Z26", f"{T0 + 1},7000.0,1,S,Z26", f"{T0 + 2},7000.0,2,U,Z26"]
    assert t.lines == 3 and t.dropped == 1 and t.errors == 0
    assert names(tmp_path) == ["MES_2026-10-28.tape"]


def test_rotates_on_trading_day_roll(tmp_path):
    t = tapelog.TapeLog(str(tmp_path), None)
    before = int(datetime(2026, 10, 28, 21, 59, tzinfo=timezone.utc).timestamp() * 1000)   # 16:59 CT
    after = int(datetime(2026, 10, 28, 22, 1, tzinfo=timezone.utc).timestamp() * 1000)     # 17:01 CT
    t.add(MES, before, 1.0, 1, 0)
    t.add(MES, after, 2.0, 1, 1)
    t.close()
    assert names(tmp_path) == ["MES_2026-10-28.tape", "MES_2026-10-29.tape"]


def test_per_file_cap_drops_and_counts(tmp_path):
    t = tapelog.TapeLog(str(tmp_path), None, max_file_mb=0)
    t.max_file = 60
    for i in range(10):
        t.add(MES, T0 + i, 7000.25, 1, 0)
    t.close()
    assert t.lines < 10 and t.dropped == 10 - t.lines
    assert (tmp_path / "MES_2026-10-28.tape").stat().st_size <= 60
    t2 = tapelog.TapeLog(str(tmp_path), None)            # a restart sees the existing size
    t2.max_file = 60
    t2.add(MES, T0, 7000.25, 1, 0)
    assert t2.dropped == 1


def test_prune_by_age_and_total(tmp_path):
    for d in ("2026-09-01", "2026-10-20", "2026-10-27", "2026-10-28"):
        (tmp_path / f"MES_{d}.tape").write_text("x" * 100)
    (tmp_path / "notes.txt").write_text("keep")
    t = tapelog.TapeLog(str(tmp_path), None, keep_days=30)
    t.prune(today=datetime(2026, 10, 28).date())
    assert not (tmp_path / "MES_2026-09-01.tape").exists() and (tmp_path / "MES_2026-10-20.tape").exists()
    t.max_total = 250
    t.prune(today=datetime(2026, 10, 28).date())
    assert names(tmp_path) == ["MES_2026-10-27.tape", "MES_2026-10-28.tape", "notes.txt"]


def test_errors_are_swallowed_and_counted(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    t = tapelog.TapeLog(str(blocker / "sub"), None)      # directory cannot be created
    t.add(MES, T0, 1.0, 1, 0)
    assert t.errors == 1 and t.lines == 0


def test_state_hook_logs_and_never_breaks_stream(tmp_path):
    log = tapelog.TapeLog(str(tmp_path), None)
    live = LiveState(tape=log)
    live._on_trade(MES, [{"timestamp": "2026-10-28T15:00:00Z", "price": 7000.5, "volume": 2, "type": 0}])
    log.close()
    assert lines(tmp_path, "MES_2026-10-28.tape")[0].endswith(",7000.5,2,B,Z26")

    class Boom:
        errors = 0

        def add(self, *a):
            raise RuntimeError("disk")
    live.tape = Boom()
    live._on_trade(MES, {"timestamp": "2026-10-28T15:00:01Z", "price": 1.0, "volume": 1, "type": 1})
    assert live.tape.errors == 1
    live.tape = None
    live._on_trade(MES, {"timestamp": "2026-10-28T15:00:02Z", "price": 1.0, "volume": 1, "type": 1})


def test_module_isolated_and_no_bare_write():
    src = (ROOT / "projectx_desk" / "tapelog.py").read_text()
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and any(x in m for x in ("guard", "client", "hub", "mcp", "app")) for m in mods)
    assert not any(isinstance(n, ast.Attribute) and n.attr in ("write", "place", "preview") for n in ast.walk(tree))


def test_tape_size_script(tmp_path):
    (tmp_path / "MES_2026-10-27.tape").write_text("a\nb\n")
    (tmp_path / "MES_2026-10-28.tape").write_text("a\n")
    spec = importlib.util.spec_from_file_location("tape_size", ROOT / "scripts" / "tape_size.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.scan(str(tmp_path))["MES"] == {"2026-10-27": (4, 2), "2026-10-28": (2, 1)}
    out = m.report(str(tmp_path))
    assert "MES" in out and "average over 2 day(s)" in out
