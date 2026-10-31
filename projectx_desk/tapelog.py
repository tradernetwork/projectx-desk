"""Raw tape log: every trade print as one line, so any tick size can be rebuilt later (the gateway keeps no tick history).

Line format (CSV, no header): `ms,price,size,side,contract` with side B (type 0, buyer aggressor), S (type 1) or U (unknown, never
guessed). One file per root per TRADING day (`tradeday.trade_date`, 17:00 CT roll): `<dir>/<ROOT>_<YYYY-MM-DD>.tape`.

Read/log only: no import of the guard, client, hub or app. Default OFF (`PX_TAPE_LOG=1` turns it on) and nothing is written until
`PX_TAPE_ROOTS` names roots (`MES,MNQ` or `*` for all), so the daily size can be measured first with `scripts/tape_size.py`.
Knobs: `PX_TAPE_DIR` (default data/tape), `PX_TAPE_KEEP_DAYS` (30, 0 = forever), `PX_TAPE_MAX_MB` (per file, 200, 0 = no cap),
`PX_TAPE_MAX_TOTAL_MB` (all files, 5000, 0 = no cap). A garbled number falls back to its default. A write error is counted and
swallowed: the trade loop never sees it. Write with `print(..., file=f)`, never a bare `.write(`.
"""
import math
import os
import re
import time
from datetime import datetime, timezone

from projectx_desk.tradeday import trade_date

_ROOT_RE = re.compile(r"^[A-Z0-9]{1,8}$")
_CODE_RE = re.compile(r"^[A-Z0-9]{1,8}$")
_FILE_RE = re.compile(r"^([A-Z0-9]{1,8})_(\d{4}-\d{2}-\d{2})\.tape$")
SIDES = {0: "B", 1: "S"}
FLUSH_EVERY = 200          # lines
FLUSH_SECONDS = 2.0


def _num(name, default, lo=0):
    raw = (os.getenv(name) or "").strip()
    try:
        v = int(raw)
        return v if v >= lo else default
    except ValueError:
        return default


def parse_cid(cid):
    """CON.F.US.MES.Z26 -> ('MES', 'Z26'); anything else -> None."""
    if not isinstance(cid, str):
        return None
    p = cid.split(".")
    if len(p) >= 5 and p[0] == "CON" and _ROOT_RE.match(p[3]) and _CODE_RE.match(p[4]):
        return p[3], p[4]
    return None


def from_env(root_dir):
    """A TapeLog, or None when PX_TAPE_LOG is not exactly 1 or no roots are named (default OFF, fails closed)."""
    if (os.getenv("PX_TAPE_LOG") or "").strip() != "1":
        return None
    raw = (os.getenv("PX_TAPE_ROOTS") or "").strip().upper()
    if not raw:
        return None
    roots = None if raw == "*" else {r.strip() for r in raw.split(",") if _ROOT_RE.match(r.strip())}
    if roots is not None and not roots:
        return None
    return TapeLog((os.getenv("PX_TAPE_DIR") or "").strip() or os.path.join(str(root_dir), "data", "tape"), roots,
                   keep_days=_num("PX_TAPE_KEEP_DAYS", 30), max_file_mb=_num("PX_TAPE_MAX_MB", 200),
                   max_total_mb=_num("PX_TAPE_MAX_TOTAL_MB", 5000))


class TapeLog:
    def __init__(self, directory, roots=None, keep_days=30, max_file_mb=200, max_total_mb=5000):
        self.dir, self.roots = directory, roots          # roots None = every root
        self.keep_days, self.max_file, self.max_total = keep_days, max_file_mb * 1048576, max_total_mb * 1048576
        self.lines = self.errors = self.dropped = 0
        self._f = None
        self._key = None                                  # (root, day) of the open file
        self._size = 0
        self._pending = 0
        self._last_flush = time.monotonic()
        self._pruned_day = None

    def add(self, cid, ts_ms, price, size, side):
        """Log one print. Never raises."""
        try:
            self._add(cid, ts_ms, price, size, side)
        except Exception:  # noqa: BLE001 - the tape log must never stop the trade loop
            self.errors += 1

    def _add(self, cid, ts_ms, price, size, side):
        rc = parse_cid(cid)
        if rc is None or (self.roots is not None and rc[0] not in self.roots):
            return
        for v in (ts_ms, price, size):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                self.dropped += 1
                return
        day = trade_date(datetime.fromtimestamp(ts_ms / 1000, timezone.utc)).isoformat()
        f = self._open(rc[0], day)
        s = SIDES.get(side, "U") if isinstance(side, int) and not isinstance(side, bool) else "U"
        line = f"{int(ts_ms)},{price!r},{size!r},{s},{rc[1]}"
        if self.max_file and self._size + len(line) + 1 > self.max_file:
            self.dropped += 1                              # per-file cap reached: drop, count, keep the loop alive
            return
        print(line, file=f)
        self._size += len(line) + 1
        self.lines += 1
        self._pending += 1
        if self._pending >= FLUSH_EVERY or time.monotonic() - self._last_flush >= FLUSH_SECONDS:
            self.flush()

    def _open(self, root, day):
        if self._key == (root, day) and self._f is not None:
            return self._f
        self.close()
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, f"{root}_{day}.tape")
        self._size = os.path.getsize(path) if os.path.exists(path) else 0
        self._f = open(path, "a", encoding="utf-8")
        self._key = (root, day)
        if self._pruned_day != day:
            self._pruned_day = day
            self.prune()
        return self._f

    def flush(self):
        try:
            if self._f is not None:
                self._f.flush()
        except Exception:  # noqa: BLE001
            self.errors += 1
        self._pending, self._last_flush = 0, time.monotonic()

    def close(self):
        try:
            if self._f is not None:
                self._f.close()
        except Exception:  # noqa: BLE001
            self.errors += 1
        self._f, self._key, self._size, self._pending = None, None, 0, 0

    def prune(self, today=None):
        """Delete day files older than keep_days, then oldest first while the total is over the cap. The open file is never removed."""
        try:
            today = today or trade_date(datetime.now(timezone.utc))
            files = []
            for name in os.listdir(self.dir):
                m = _FILE_RE.match(name)
                if m:
                    p = os.path.join(self.dir, name)
                    files.append((m.group(2), p, os.path.getsize(p)))
            files.sort()
            open_name = f"{self._key[0]}_{self._key[1]}.tape" if self._key else None
            keep = []
            for day, p, sz in files:
                age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
                if self.keep_days and age > self.keep_days and os.path.basename(p) != open_name:
                    os.remove(p)
                else:
                    keep.append((day, p, sz))
            total = sum(sz for _, _, sz in keep)
            for day, p, sz in keep:
                if not self.max_total or total <= self.max_total:
                    break
                if os.path.basename(p) != open_name:
                    os.remove(p)
                    total -= sz
        except Exception:  # noqa: BLE001
            self.errors += 1

    def status(self):
        return {"on": True, "lines": self.lines, "errors": self.errors, "dropped": self.dropped}
