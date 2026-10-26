"""Grade log: dedupe, rotation, swallowed failures, numbers-only entries."""
import json

from projectx_desk import grades as G
from projectx_desk import indicators as ind

CID = "CON.F.US.MES.Z26"


def ev(bar_ts=1000, **over):
    d = {"asof": bar_ts, "price": 7000.25, "atr": 4.5, "macd_pctile": 0.8, "rsi": 55.2,
         "levels": [{"id": "ema34"}],
         "candidates": [{"id": "ema34", "held": 12, "tests": 31, "hold_rate": 61.0, "placebo_rate": 40.0, "p_value": 0.01,
                         "fdr": True, "verdict": "ABOVE_RANDOM", "stable": True, "label": "free text must not travel"},
                        {"id": "evil id", "held": 1}]}
    d.update(over)
    return d


def test_entry_shape_and_allowlist():
    e = G.build_entry(5, CID, "5m", ev())
    assert set(e) == {"ts", "contract", "tf", "bar_ts", "price", "atr", "candidates", "shown", "osc"}
    assert e["shown"] == ["ema34"] and e["osc"] == {"macd_pctile": 0.8, "rsi": 55.2}
    assert [c["id"] for c in e["candidates"]] == ["ema34"]
    assert set(e["candidates"][0]) == {"id", "held", "tests", "hold_rate", "placebo", "p", "fdr", "verdict", "stable"}
    assert "free text" not in json.dumps(e)
    assert G.build_entry(5, "bad", "5m", ev()) is None and G.build_entry(5, CID, "5m", ev(asof=None)) is None


def test_dedupe_per_bar(tmp_path):
    g = G.GradeLog(tmp_path / "sub" / "g.jsonl")
    assert g.log_eval(CID, "5m", ev(1000), 1) is True
    assert g.log_eval(CID, "5m", ev(1000), 2) is False
    assert g.log_eval(CID, "15m", ev(1000), 3) is True
    assert g.log_eval(CID, "5m", ev(1300), 4) is True
    lines = (tmp_path / "sub" / "g.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(json.loads(x)["contract"] == CID for x in lines)


def test_rotation_replaces_old_dot_one(tmp_path):
    p = tmp_path / "g.jsonl"
    g = G.GradeLog(p, max_bytes=300)
    n = 0
    while not (tmp_path / "g.jsonl.1").exists() and n < 50:
        n += 1
        g.log_eval(CID, "5m", ev(1000 + n), n)
    assert (tmp_path / "g.jsonl.1").exists()
    old = (tmp_path / "g.jsonl.1").read_text(encoding="utf-8")
    assert p.exists() and p.stat().st_size < 300 + 2000
    while (tmp_path / "g.jsonl.1").read_text(encoding="utf-8") == old and n < 200:     # a second rotation replaces the first .1
        n += 1
        g.log_eval(CID, "5m", ev(1000 + n), n)
    assert (tmp_path / "g.jsonl.1").read_text(encoding="utf-8") != old


def test_write_failure_swallowed_one_warning(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    g = G.GradeLog(blocker / "g.jsonl")            # parent is a file: mkdir fails
    assert g.log_eval(CID, "5m", ev(1), 1) is False
    assert g.log_eval(CID, "5m", ev(2), 2) is False
    out = capsys.readouterr().out
    assert out.count("grade log write failed") == 1
    assert "file" not in out.replace("grade log write failed", "")


def test_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_GRADES_FILE", str(tmp_path / "x.jsonl"))
    assert G.GradeLog.from_env(tmp_path).path == tmp_path / "x.jsonl"
    monkeypatch.delenv("PX_GRADES_FILE")
    assert G.GradeLog.from_env(tmp_path).path == tmp_path / "data" / "indicator_grades.jsonl"


def test_restart_does_not_relog_a_bar_already_on_disk(tmp_path):
    p = tmp_path / "g.jsonl"
    first = G.GradeLog(p)
    assert first.log({"contract": CID, "tf": "bias", "bar_ts": 300_000, "v": 1}) is True
    again = G.GradeLog(p)                                                  # a restart: new object, same file
    assert again.log({"contract": CID, "tf": "bias", "bar_ts": 300_000, "v": 2}) is False
    assert again.log({"contract": CID, "tf": "bias", "bar_ts": 600_000, "v": 3}) is True
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2


def test_seeding_survives_a_missing_file_and_a_garbled_line(tmp_path):
    assert G.GradeLog(tmp_path / "nope.jsonl").log({"contract": CID, "tf": "bias", "bar_ts": 1}) is True
    p = tmp_path / "g.jsonl"
    p.write_text('not json\n{"contract":"%s","tf":"bias","bar_ts":7}\n[1,2]\n' % CID, encoding="utf-8")
    g = G.GradeLog(p)
    assert g.seen(CID, "bias", 7) and g.log({"contract": CID, "tf": "bias", "bar_ts": 7}) is False


def test_a_days_old_closed_bar_is_stale_for_the_bias_log():
    from projectx_desk.app import bias_bar_is_stale
    now = 1_000_000_000_000
    assert bias_bar_is_stale(now - 300_000, now) is False             # the bar that just closed
    assert bias_bar_is_stale(now - 900_000, now) is False             # a slow bars API
    assert bias_bar_is_stale(now - 176_700_000, now) is True          # the Friday bar on Sunday
    assert bias_bar_is_stale(None, now) is True and bias_bar_is_stale("x", now) is True
