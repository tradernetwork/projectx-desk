"""Append-only grade log of what the indicator selection showed, for later grading (did shown levels hold?).

JSONL at data/indicator_grades.jsonl (PX_GRADES_FILE). One line per (contract, tf, last closed bar ts); a rescan of the
same closed bar logs nothing. Numbers and catalog ids only: no account id, no balance, no free text. A write failure is
swallowed after ONE printed warning; this module must never break the tracking loop or a request. Rotation: past
MAX_BYTES the file is renamed to <file>.1 (replacing any old one) and a fresh file starts.
"""
import json
import math
import os
import pathlib
import re
import time

from projectx_desk.respect import CATALOG

MAX_BYTES = 50 * 1024 * 1024
_SEEN_CAP = 4000
_CID_RE = re.compile(r"^CON\.[A-Za-z0-9._]{1,40}$")
_VERDICT_RE = re.compile(r"^[A-Z][A-Z_]{0,23}$")


def _n(x, nd=4):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(float(x), nd) if isinstance(x, float) else x


def _b(x):
    return x if isinstance(x, bool) else None


def build_entry(now_ms, cid, tf, ev):
    """The log line for one (contract, tf) from indicators.evaluate output. None when the contract or bar is unusable."""
    if not isinstance(cid, str) or not _CID_RE.match(cid) or not isinstance(ev, dict) or ev.get("asof") is None:
        return None
    cands = []
    for r in ev.get("candidates") or []:
        if not isinstance(r, dict) or r.get("id") not in CATALOG:
            continue
        v = r.get("verdict")
        cands.append({"id": r["id"], "held": _n(r.get("held")), "tests": _n(r.get("tests")),
                      "hold_rate": _n(r.get("hold_rate")), "placebo": _n(r.get("placebo_rate")),
                      "p": _n(r.get("p_value")), "fdr": _b(r.get("fdr")),
                      "verdict": v if isinstance(v, str) and _VERDICT_RE.match(v) else None, "stable": _b(r.get("stable"))})
    return {"ts": int(now_ms), "contract": cid, "tf": tf, "bar_ts": int(ev["asof"]), "price": _n(ev.get("price")),
            "atr": _n(ev.get("atr")), "candidates": cands,
            "shown": [l["id"] for l in ev.get("levels") or [] if l.get("id") in CATALOG],
            "osc": {"macd_pctile": _n(ev.get("macd_pctile")), "rsi": _n(ev.get("rsi"))}}


class GradeLog:
    def __init__(self, path, max_bytes=MAX_BYTES):
        self.path = pathlib.Path(path)
        self.max_bytes = int(max_bytes)
        self._seen = {}
        self._warned = False

    @classmethod
    def from_env(cls, root):
        return cls(os.getenv("PX_GRADES_FILE") or (pathlib.Path(root) / "data" / "indicator_grades.jsonl"))

    def seen(self, cid, tf, bar_ts):
        return (cid, tf, bar_ts) in self._seen

    def log(self, entry) -> bool:
        """Append `entry` unless that (contract, tf, bar_ts) is already logged. True when a line was written."""
        if not isinstance(entry, dict):
            return False
        key = (entry.get("contract"), entry.get("tf"), entry.get("bar_ts"))
        if key in self._seen:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                print(json.dumps(entry, separators=(",", ":"), allow_nan=False), file=f)
        except Exception as e:  # noqa: BLE001 - logging must never break the loop
            if not self._warned:
                self._warned = True
                print(f"grade log write failed: {type(e).__name__}", flush=True)
            return False
        if len(self._seen) >= _SEEN_CAP:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = True
        return True

    def log_eval(self, cid, tf, ev, now_ms=None) -> bool:
        e = build_entry(now_ms if now_ms is not None else int(time.time() * 1000), cid, tf, ev)
        return self.log(e) if e else False
