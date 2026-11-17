"""The Tomorrow's Map record: each night's map for each tracked contract, and its grade against the next regular session.

Append-only JSON lines in data/tomorrow_ledger.jsonl (PX_TOMORROW_LEDGER): `map` rows (what was drawn, before anyone knew the outcome) and
`grade` rows (what the session then did, scored by tomorrow_grade). Numbers, enums and our own generated sentences only; no vendor text,
no account data. Display and logging only: nothing here is read by the guard or by sizing.

Skips are never silent: a pass that cannot file a map (a non-OK result, a snapshot older than MAX_SNAPSHOT_AGE_S) logs the reason
(root, status, code, age_s) to the desk log and to data/tomorrow_ledger_errors.jsonl (PX_TOMORROW_ERRORS), once per change of reason.
After a hard deadline (18:00 CT on the weekday) a tracked root with no map gets an explicit `missed` row instead of silent absence; the
first tick after a weekend or an overnight gap writes the previous weekday's misses. The grader ignores `missed` rows, the summary counts them.

Recording: after the cash close (15:10 CT, Monday to Friday), once per (root, session, map version), for the session drawn FOR (the next
weekday; holidays are not modelled, so a map for a holiday simply never gets bars and is never graded). Grading: once that session's
08:30 to 15:00 CT bars exist (after 15:05 CT on the day, or any later day, for six days), from the futures contract the map was drawn in."""
import json
import os
import pathlib
import re
from datetime import date, datetime, timedelta

from projectx_desk import tomorrow as T
from projectx_desk import tomorrow_grade as G
from projectx_desk import tomorrow_feed as F
from projectx_desk.tradeday import CT

RECORD_AFTER = (15, 10)           # CT: after the 15:00 cash close, so "survives tonight" is true
GRADE_AFTER = (15, 5)
GRADE_WINDOW_DAYS = 6
MAX_SNAPSHOT_AGE_S = 6 * 3600     # never file an old snapshot as tonight's map
MAX_LINES = 4000
DEADLINE = (18, 0)                # CT: a map not filed by now is recorded as missed, and none is filed later
_CODE = re.compile(r"[A-Z0-9_]{1,40}")
_skips = {}                       # (root, session) -> (reason, status, code) last logged, so a stuck feed is one line, not one per pass


def path_from_env(root):
    return pathlib.Path(os.getenv("PX_TOMORROW_LEDGER") or (pathlib.Path(root) / "data" / "tomorrow_ledger.jsonl"))


def errors_path(ledger_path):
    """PX_TOMORROW_ERRORS, else tomorrow_ledger_errors.jsonl beside the ledger."""
    env = os.getenv("PX_TOMORROW_ERRORS")
    return pathlib.Path(env) if env else pathlib.Path(ledger_path).with_name("tomorrow_ledger_errors.jsonl")


def _word(v):
    return v if isinstance(v, str) and _CODE.fullmatch(v) else None


def _age(v):
    return round(v, 1) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf") else None


def log_skip(path, root, session, reason, res, now_ts):
    """One line for a filing that did not happen. Allowlisted fields only (enums, numbers): never the vendor's text."""
    status, code = _word(res.get("status")), _word(res.get("code"))
    key = (reason, status, code)
    if _skips.get((root, session)) == key:
        return
    _skips[(root, session)] = key
    age = _age(res.get("age_s"))
    print(f"tomorrow ledger: skipped {root} session={session} reason={reason} status={status} code={code} age_s={age}", flush=True)
    try:
        append(errors_path(path), {"ts": int(now_ts), "root": root, "session": session, "reason": reason, "status": status, "code": code, "age_s": age})
    except OSError:
        pass


def next_session(d):
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def load(path, max_lines=MAX_LINES):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()[-max_lines:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("kind") in ("map", "grade", "missed"):
            out.append(r)
    return out


def append(path, entry):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        print(json.dumps(entry, separators=(",", ":"), allow_nan=False), file=f)


def has(rows, kind, root, session, map_version):
    return any(r["kind"] == kind and r.get("root") == root and r.get("session") == session and r.get("map_version") == map_version for r in rows)


def summarize(rows, root):
    """The last call and running record for one root (tomorrow_grade.record), from the graded rows only."""
    out = G.record([{"session": r["session"], "map_version": r.get("map_version"), "grade_version": r.get("grade_version"), "grade": r.get("grade")}
                     for r in rows if r["kind"] == "grade" and r.get("root") == root and isinstance(r.get("session"), str)])
    out["record"]["missed"] = sum(1 for r in rows if r["kind"] == "missed" and r.get("root") == root)
    return out


def due_grades(rows, now):
    """Map rows whose session is over, within the grading window, with no grade yet. `now` is a CT-aware datetime."""
    out = []
    for r in rows:
        if r["kind"] != "map" or has(rows, "grade", r.get("root"), r.get("session"), r.get("map_version")):
            continue
        try:
            sd = date.fromisoformat(r["session"])
        except (KeyError, ValueError, TypeError):
            continue
        over = sd < now.date() or (sd == now.date() and (now.hour, now.minute) >= GRADE_AFTER)
        if over and sd >= now.date() - timedelta(days=GRADE_WINDOW_DAYS):
            out.append(r)
    return out


def missed_nights(now):
    """Weekdays whose filing deadline has passed and is still ours to settle: today after 18:00, and the previous weekday when the
    first look comes after a weekend or an overnight gap (before today's 15:10, when the desk was quiet at 18:00)."""
    out = []
    if now.weekday() < 5 and (now.hour, now.minute) >= DEADLINE:
        out.append(now.date())
    if now.weekday() >= 5 or (now.hour, now.minute) < RECORD_AFTER:
        d = now.date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        out.append(d)
    return out


def write_missed(path, rows, tracked, now, now_ts):
    for d in missed_nights(now):
        session = next_session(d).isoformat()
        for root, cid in tracked.items():
            if not cid or has(rows, "map", root, session, T.VERSION) or has(rows, "missed", root, session, T.VERSION):
                continue
            why = _skips.get((root, session), ("NOT_FILED",))[0]
            append(path, {"kind": "missed", "root": root, "contract": cid, "made_after": d.isoformat(), "session": session, "map_version": T.VERSION,
                          "made_at": int(now_ts), "deadline": "18:00CT", "reason": why})
            rows.append({"kind": "missed", "root": root, "session": session, "map_version": T.VERSION})
            print(f"tomorrow ledger: MISSED {root} session={session} reason={why}", flush=True)


async def tick(app, path, now_ts, tracked=None):
    """One pass: record tonight's maps when due, then grade whatever sessions have finished. Never raises past the caller's guard."""
    now = datetime.fromtimestamp(now_ts, CT)
    rows = load(path)
    tracked = tracked if tracked is not None else (getattr(app.state, "tracked", None) or {})
    hm = (now.hour, now.minute)
    if now.weekday() < 5 and RECORD_AFTER <= hm < DEADLINE:
        session = next_session(now.date()).isoformat()
        for root, cid in tracked.items():
            if not cid or has(rows, "map", root, session, T.VERSION):
                continue
            res = await F.tomorrow_result(app, cid, now=now_ts)
            reason = ("NOT_OK" if res.get("status") != "OK" else "NO_MAP" if not isinstance(res.get("map"), dict)
                      else "STALE" if (res.get("age_s") or 0) > MAX_SNAPSHOT_AGE_S else None)
            if reason:
                log_skip(path, root, session, reason, res, now_ts)
                continue
            append(path, {"kind": "map", "root": root, "contract": cid, "made_after": now.date().isoformat(), "session": session, "map_version": T.VERSION,
                          "made_at": int(now_ts), "snapshot_epoch": res.get("snapshot_epoch"), "source": res.get("source"), "ratio": res.get("ratio"),
                          "map": res["map"]})
            rows = load(path)
    rows = load(path)
    write_missed(path, rows, tracked, now, now_ts)
    for r in due_grades(rows, now):
        cid = r.get("contract")
        bar = await F.session_bar(app, cid, date.fromisoformat(r["session"])) if cid else None
        if bar is None:
            continue
        g = G.grade_map(r["map"], bar)
        append(path, {"kind": "grade", "root": r["root"], "session": r["session"], "map_version": r["map_version"], "grade_version": G.GRADE_VERSION,
                      "graded_at": int(now_ts), "bar": bar, "grade": g})
