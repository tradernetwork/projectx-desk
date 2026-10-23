"""Real buy/sell volume from the ProjectX trade tape (GatewayTrade `type`: 0 = buyer aggressed, 1 = seller aggressed).

Trades are summed into 1-minute buckets per contract. With a `path` the store is a SQLite file (`data/flow.db`) that keeps
EVERY minute, hole and partial flag across restarts (no two-day cap; `PX_FLOW_KEEP_DAYS` prunes, default 90, 0 = forever); the
most recent `MAX_MINUTES` per contract stay in memory as the hot window. Without a path it is pure and in memory. ProjectX has no historical tick endpoint, so
this only covers time the desk was subscribed: `since` says where coverage starts, and any minute that touched a
hub reconnect or a malformed trade is flagged `partial` and never drawn as if it were complete. Volume of unknown
side is counted separately (`u`), never guessed into buy or sell. Numbers only; nothing from the gateway is echoed.
"""
import json
import math
import os
import sqlite3
import threading
import time

MIN_MS = 60_000
MAX_MINUTES = 3000          # the in-memory hot window per contract (about two trading days); the database keeps the rest
SERVE_MINUTES = 12_000      # most minutes one snapshot returns (about eight active trading days)
KEEP_DAYS = 90
MAX_CONTRACTS = 8
MAX_TRADE_VOL = 100_000
MAX_HOLES = 200
SHORT_GAP_MS = 15_000      # a reconnect that resumes within this loses a few percent of one bar's trades at most: it is not worth blanking the bar
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS minutes(cid TEXT NOT NULL, t INTEGER NOT NULL, buy INTEGER NOT NULL, sell INTEGER NOT NULL, "
    "unk INTEGER NOT NULL, partial INTEGER NOT NULL, PRIMARY KEY(cid, t)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS holes(cid TEXT NOT NULL, lo INTEGER NOT NULL, hi INTEGER NOT NULL, PRIMARY KEY(cid, lo, hi)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS meta(cid TEXT PRIMARY KEY, since INTEGER NOT NULL, last_ts INTEGER NOT NULL) WITHOUT ROWID",
)
TS_MIN, TS_MAX = 946_684_800_000, 4_102_444_800_000      # 2000-01-01 .. 2100-01-01 in ms: anything else is not a real trade time


def _keep_days(v=None):
    """PX_FLOW_KEEP_DAYS: whole days of minutes to keep. 0 = forever; blank or garbled falls back to the default."""
    raw = os.getenv("PX_FLOW_KEEP_DAYS") if v is None else v
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return KEEP_DAYS
    return n if 0 <= n <= 36_500 else KEEP_DAYS


def _okts(v):
    return isinstance(v, int) and not isinstance(v, bool) and TS_MIN <= v <= TS_MAX


def _okvol(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10**9


class FlowStore:
    """`holes` are [from_minute, to_minute] stretches where trades were NOT being received (reconnect, restart, closed
    subscription); a bar that overlaps one is never drawn. With `path` (a SQLite file) every minute survives a restart: dirty
    minutes are flushed every `save_every_s` while trades flow, and on load every contract starts flagged so the downtime
    becomes a hole instead of a quiet-looking stretch of zeros. `legacy_json` is the old flow.json, imported once."""

    def __init__(self, path=None, save_every_s=30.0, legacy_json=None, keep_days=None):
        self._c = {}        # cid -> {"since", "m": {minute_ms: [buy, sell, unknown]}, "partial": set, "gap": bool, "holes": [], "last_ts"}
        self._path, self._every, self._last_save = path, save_every_s, time.monotonic()
        self._keep_days = _keep_days(keep_days)
        self._db, self._lock = None, threading.RLock()
        self._dirty, self._new_holes, self._pending = {}, [], []     # minutes to flush, holes to insert, rows evicted while still dirty
        if path:
            self._open()
            if self._db is not None:
                self._load()
                if legacy_json and not self._c:
                    self._import_json(legacy_json)

    def _cid(self, cid):
        if cid not in self._c:
            if len(self._c) >= MAX_CONTRACTS:
                self._c.pop(next(iter(self._c)))
            self._c[cid] = {"since": None, "m": {}, "partial": set(), "gap": False, "holes": [], "last_ts": None}
        return self._c[cid]

    # ---- persistence ----------------------------------------------------------------------
    def _connect(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        db = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            for stmt in _SCHEMA:
                db.execute(stmt)
            db.commit()
            db.execute("SELECT 1 FROM meta LIMIT 1").fetchall()
        except sqlite3.Error:
            db.close()
            raise
        return db

    def _open(self):
        """Open the database. An unreadable file is moved aside (never deleted) and replaced by a fresh one; anything else
        that goes wrong leaves the store in memory only, which is what the desk did before it had a database."""
        try:
            self._db = self._connect()
            return
        except sqlite3.DatabaseError:
            pass
        except (sqlite3.Error, OSError):
            self._db = None
            return
        try:
            os.replace(self._path, f"{self._path}.corrupt-{int(time.time())}")
            self._db = self._connect()
        except (sqlite3.Error, OSError):
            self._db = None

    def _load(self):
        """Trust nothing in the file: types, ranges and sizes are re-checked, and anything off is skipped."""
        try:
            with self._lock:
                if self._keep_days:
                    self._prune(int(time.time() * 1000))
                metas = self._db.execute("SELECT cid, since, last_ts FROM meta ORDER BY last_ts DESC LIMIT ?", (MAX_CONTRACTS,)).fetchall()
                for cid, since, last in metas:
                    if not (isinstance(cid, str) and _okts(since) and _okts(last)):
                        continue
                    rows = self._db.execute("SELECT t, buy, sell, unk, partial FROM minutes WHERE cid=? ORDER BY t DESC LIMIT ?",
                                            (cid, MAX_MINUTES)).fetchall()
                    m, partial = {}, set()
                    for t, b, s, u, p in rows:
                        if not (_okts(t) and _okvol(b) and _okvol(s) and _okvol(u)):
                            continue
                        if b or s or u:
                            m[t] = [b, s, u]
                        if p:
                            partial.add(t)
                    if not m:
                        continue                                   # a contract with no usable minutes is dropped, not repaired
                    holes = [[a, b] for a, b in self._db.execute("SELECT lo, hi FROM holes WHERE cid=? ORDER BY lo DESC LIMIT ?",
                                                                 (cid, MAX_HOLES)).fetchall() if _okts(a) and _okts(b)][::-1]
                    self._c[cid] = {"since": since, "m": dict(sorted(m.items())), "partial": partial, "gap": True,   # not listening while the desk was down
                                    "holes": holes, "last_ts": last}
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            self._c = {}          # unreadable or wrongly-shaped rows: start clean rather than trust them

    def _prune(self, now_ms):
        cut = int(now_ms) - self._keep_days * 86_400_000
        self._db.execute("DELETE FROM minutes WHERE t < ?", (cut,))
        self._db.execute("DELETE FROM holes WHERE hi < ?", (cut,))
        self._db.commit()

    def _import_json(self, path):
        """One-time import of the old flow.json. The file is renamed afterwards so it is never imported twice."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for cid, c in list((raw.get("c") or {}).items())[:MAX_CONTRACTS]:
                m = {}
                for k, v in (c.get("m") or {}).items():
                    k = int(k)
                    if _okts(k) and isinstance(v, list) and len(v) >= 3 and all(_okvol(x) for x in v[:3]):
                        m[k] = [int(x) for x in v[:3]]
                since, last = c.get("since"), c.get("last_ts")
                if not (_okts(since) and _okts(last)) or not m:
                    continue
                holes = [[int(a), int(b)] for a, b in (c.get("holes") or []) if _okts(a) and _okts(b)]
                partial = {int(x) for x in (c.get("partial") or []) if _okts(x)}
                self._c[str(cid)] = {"since": since, "m": dict(sorted(m.items())[-MAX_MINUTES:]), "partial": partial, "gap": True,
                                     "holes": holes[-MAX_HOLES:], "last_ts": last}
                self._dirty[str(cid)] = set(m) | partial
                self._new_holes += [(str(cid), a, b) for a, b in holes]
            self.save(force=True)
            os.replace(path, f"{path}.migrated")
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            self._c = {}          # unreadable or wrongly-shaped legacy file: start clean rather than trust it

    def save(self, force=False):
        if not self._path or self._db is None:
            return
        try:
            with self._lock:
                rows = list(self._pending)
                for cid, minutes in self._dirty.items():
                    c = self._c.get(cid)
                    if c is None:
                        continue
                    for k in minutes:
                        b = c["m"].get(k)
                        if b is not None or k in c["partial"]:
                            rows.append((cid, k, *(b or (0, 0, 0)), 1 if k in c["partial"] else 0))
                self._db.executemany("INSERT OR REPLACE INTO minutes(cid, t, buy, sell, unk, partial) VALUES (?,?,?,?,?,?)", rows)
                self._db.executemany("INSERT OR IGNORE INTO holes(cid, lo, hi) VALUES (?,?,?)", self._new_holes)
                self._db.executemany("INSERT OR REPLACE INTO meta(cid, since, last_ts) VALUES (?,?,?)",
                                     [(cid, c["since"], c["last_ts"]) for cid, c in self._c.items()
                                      if c["since"] is not None and c["last_ts"] is not None])
                self._db.commit()
                self._dirty, self._new_holes, self._pending = {}, [], []
        except (sqlite3.Error, OSError, TypeError):
            pass                  # never let a full disk or a locked file stop the tape; the dirty set is kept and retried
        self._last_save = time.monotonic()

    def add(self, cid, ts_ms, vol, side):
        """One trade. A bad number marks its minute partial instead of being silently dropped."""
        if isinstance(ts_ms, bool) or not isinstance(ts_ms, (int, float)) or not math.isfinite(ts_ms) or not TS_MIN <= ts_ms <= TS_MAX:
            self.mark_gap(cid)                  # an unusable timestamp means a trade we cannot place: its minute is unknown
            return
        c = self._cid(cid)
        k = int(ts_ms) // MIN_MS * MIN_MS
        if self._db is not None:
            self._dirty.setdefault(cid, set()).add(k)
        if c["since"] is None:
            c["since"] = k + MIN_MS            # the first minute is entered mid-way: coverage starts at the next one
            c["partial"].add(k)
        if c["gap"]:
            c["gap"] = False
            if c["last_ts"] is None or ts_ms - c["last_ts"] > SHORT_GAP_MS:      # a brief blip is tolerated; a real outage is a hole
                c["partial"].add(k)            # trades were lost before this one
                if c["last_ts"] is not None and k > c["last_ts"] // MIN_MS * MIN_MS:
                    c["partial"].add(c["last_ts"] // MIN_MS * MIN_MS)
                    c["holes"] = (c["holes"] + [[c["last_ts"] // MIN_MS * MIN_MS, k]])[-MAX_HOLES:]
                    if self._db is not None:
                        self._dirty[cid].add(c["last_ts"] // MIN_MS * MIN_MS)
                        self._new_holes.append((cid, c["last_ts"] // MIN_MS * MIN_MS, k))
        c["last_ts"] = int(ts_ms) if c["last_ts"] is None else max(c["last_ts"], int(ts_ms))
        if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not math.isfinite(vol) or not 0 < vol <= MAX_TRADE_VOL \
                or vol != int(vol):                    # futures trade in whole contracts: a fraction is a bad print, never silently truncated
            c["partial"].add(k)
            if len(c["partial"]) > MAX_MINUTES:           # invalid trades create no bucket, so eviction never prunes these
                c["partial"] = set(sorted(c["partial"])[-MAX_MINUTES:])
            return
        if len(c["partial"]) > MAX_MINUTES:
            c["partial"] = set(sorted(c["partial"])[-MAX_MINUTES:])
        b = c["m"].setdefault(k, [0, 0, 0])
        b[0 if side == 0 else 1 if side == 1 else 2] += int(vol)
        if len(c["m"]) > MAX_MINUTES:
            old = min(c["m"])
            if self._db is not None and old in self._dirty.get(cid, ()):
                self._pending.append((cid, old, *c["m"][old], 1 if old in c["partial"] else 0))   # still unsaved: it must not be lost with the eviction
                self._dirty[cid].discard(old)
            del c["m"][old]
            c["partial"].discard(old)
            if self._db is None:                       # in memory only, evicting a minute ends the coverage that started before it
                c["since"] = max(c["since"], old + 2 * MIN_MS)
            c["holes"] = [h for h in c["holes"] if h[1] > old]
        if self._path and time.monotonic() - self._last_save >= self._every:
            self.save()

    def mark_gap(self, cid=None, now_ms=None):
        if now_ms is not None and (isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms)):
            now_ms = None
        """A hub reconnect/resync: trades may have been lost. When trades resume after more than SHORT_GAP_MS, the minute of the last trade
        and the first minute back are partial and the stretch between them is a hole."""
        for key in ([cid] if cid is not None else list(self._c)):
            c = self._c.get(key)
            if c is None or c["since"] is None:
                continue
            c["gap"] = True      # whether it costs a bar is decided when trades resume: a blip of 15 s or less is tolerated (SHORT_GAP_MS)

    def _history(self, cid, below, after):
        """Rows and holes older than the in-memory window, straight from the database. `below` is the oldest minute held in
        memory (None = nothing in memory), `after` an optional lower bound. Never raises."""
        if self._db is None:
            return [], []
        try:
            with self._lock:
                hi = (below if below is not None else TS_MAX + 1) - 1
                lo = after if after is not None else 0
                rows = self._db.execute("SELECT t, buy, sell, unk, partial FROM minutes WHERE cid=? AND t>=? AND t<=? ORDER BY t DESC LIMIT ?",
                                        (cid, lo, hi, SERVE_MINUTES)).fetchall()
                first = rows[-1][0] if rows else lo
                holes = self._db.execute("SELECT lo, hi FROM holes WHERE cid=? AND hi>=? ORDER BY lo", (cid, max(first, lo) - MIN_MS)).fetchall()
        except sqlite3.Error:
            return [], []
        rows = [[t, b, s, u, 1 if p else 0] for t, b, s, u, p in reversed(rows) if _okts(t) and _okvol(b) and _okvol(s) and _okvol(u)]
        return rows, [[a, b] for a, b in holes if _okts(a) and _okts(b)]

    def snapshot(self, cid, now_ms=None, after=None):
        """`holes` = the stored closed holes plus, while a gap is flagged and no trade has arrived since, one derived OPEN
        hole [last trade minute, now minute]: an outage that is still going on must not age into a quiet-looking window.
        `after` (ms) returns only minutes from then on, so a poller can fetch the tail instead of the whole series."""
        c = self._c.get(cid)
        if not c or c["since"] is None:
            return {"v": 1, "since": None, "minutes": [], "holes": []}
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            after = None
        rows = [[k, b[0], b[1], b[2], 1 if k in c["partial"] else 0] for k, b in sorted(c["m"].items())]
        have = set(c["m"])
        rows += [[k, 0, 0, 0, 1] for k in sorted(c["partial"]) if k not in have]      # a bad print leaves a partial minute with no bucket
        rows.sort(key=lambda r: r[0])
        holes = [list(h) for h in c["holes"]]
        if self._db is not None and (after is None or not rows or after < rows[0][0]):
            old_rows, old_holes = self._history(cid, rows[0][0] if rows else None, after)
            room = max(0, SERVE_MINUTES - len(rows))
            rows = old_rows[-room:] + rows if room else rows
            holes = sorted({tuple(h) for h in old_holes + holes})
            holes = [list(h) for h in holes]
        if after is not None:
            rows = [r for r in rows if r[0] >= after]
            holes = [h for h in holes if h[1] + MIN_MS > after]
        if c["gap"] and c["last_ts"] is not None:
            if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms) or now_ms < c["last_ts"]:
                now_ms = time.time() * 1000                                  # an unusable "now" (or one before the last trade) is replaced by the real clock
            if now_ms - c["last_ts"] > SHORT_GAP_MS:                         # the outage is still going on and is no longer a blip
                lo = c["last_ts"] // MIN_MS * MIN_MS
                holes.append([lo, max(lo, int(now_ms) // MIN_MS * MIN_MS)])  # derived, never stored
        return {"v": 1, "since": c["since"], "minutes": rows, "holes": holes}     # row = [minute_ms, buy, sell, unknown, partial]


WINDOWS_MIN = (5, 15, 60)


def summary(store, cid, now_ms):
    """Trailing windows for the AI surface: {since, w: {minutes: {buy, sell, unknown, delta, complete}}}. A window is
    `complete` only if coverage started before it and no minute in it is partial. Numbers only."""
    if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms):
        now_ms = time.time() * 1000
    snap = store.snapshot(cid, now_ms, after=int(now_ms) - (max(WINDOWS_MIN) + 1) * MIN_MS)
    since, rows, holes = snap["since"], snap["minutes"], snap["holes"]
    if since is None:
        return None
    out = {}
    end = int(now_ms) // MIN_MS * MIN_MS
    for w in WINDOWS_MIN:
        start = end - (w - 1) * MIN_MS
        buy = sell = unk = 0
        partial = False
        for t, b, s, u, p in rows:
            if start <= t <= end:
                buy, sell, unk, partial = buy + b, sell + s, unk + u, partial or bool(p)
        out[w] = {"buy": buy, "sell": sell, "unknown": unk, "delta": buy - sell, "complete": since <= start and not partial
                    and not any(a < end + MIN_MS and b + MIN_MS > start for a, b in holes)}
    return {"since": since, "w": out}
