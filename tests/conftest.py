"""projectx tests: the `projectx_desk` package lives one directory up, so put the checkout root on sys.path."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """No test may touch the real data/ (audit log, halt file, day state, flow, desk lock). Every path env var the code
    reads points at a per-test tmp dir. A test that sets one itself (monkeypatch.setenv or env=) still wins, because it
    runs after this fixture. Limits() built without from_env falls back to limits.HERE / "data", so that moves too."""
    d = tmp_path / "px_runtime"
    d.mkdir()
    for var, name in (("PX_AUDIT_PATH", "audit.jsonl"), ("PX_STATE_PATH", "day_state.json"),
                      ("PX_INTENT_PATH", "intents.json"), ("PX_HALT_FILE", "HALT"),
                      ("PX_FLOW_FILE", "flow.json"), ("PX_TICKS_FILE", "ticks.json"), ("PX_LOCK_FILE", "desk.lock"),
                      ("PX_GRADES_FILE", "indicator_grades.jsonl"), ("PX_BIAS_LOG_FILE", "bias_log.jsonl"), ("PX_VAPID_FILE", "vapid.json"),
                      ("PX_PUSH_FILE", "push_subs.json")):
        monkeypatch.setenv(var, str(d / name))
    from projectx_desk import limits
    monkeypatch.setattr(limits, "HERE", d.parent / "px_root")
