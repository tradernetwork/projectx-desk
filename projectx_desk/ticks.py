"""Tick bars (N consecutive trade PRINTS per bar) built from the ProjectX trade tape.

Pure, no I/O. retrieveBars has no tick unit and the gateway has no tick history, so bars exist only for time the desk
was subscribed (`since`). Same honesty rules as px_flow: nothing is invented or interpolated. A hub reconnect or a
malformed print flags the bar it landed in `gap` (it may be missing prints) and the print itself is not counted.
Volume of unknown aggressor side (`type` not 0/1) is kept apart (`unk`), never guessed. Numbers only.

With a `path` the completed bars survive a restart (PX_TICKS_FILE): the file is rewritten every `save_every_s` while prints
flow and on a clean stop. Loading closes any forming bar as a short bar flagged `gap` and flags the next print, so the
downtime is a hole in the series, never a bridged bar. A corrupt or wrong-version file is ignored with one warning.
"""
import json
import math
import os
import time
from collections import deque

DEFAULT_SIZES = (1024, 2048, 4096)
MIN_N, MAX_N, MAX_SIZES = 50, 50_000, 4
MAX_BARS = 5000             # completed bars kept (and persisted) per contract per N
MAX_CONTRACTS = 8
MAX_TRADE_VOL = 100_000
TS_MIN, TS_MAX = 946_684_800_000, 4_102_444_800_000      # 2000-01-01 .. 2100-01-01 in ms: anything else is not a real trade time
MAX_PX = 10_000_000


def parse_sizes(raw=None):
    """`PX_TICK_BARS` -> tuple of distinct ints clamped to MIN_N..MAX_N, at most MAX_SIZES. Garbage entries are skipped;
    nothing usable (or more than MAX_SIZES distinct values) falls back to the default."""
    if raw is None:
        raw = os.environ.get("PX_TICK_BARS", "")
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        try:
            n = min(MAX_N, max(MIN_N, int(part)))
        except ValueError:                      # '²' passes str.isdigit() but not int(): skip any garbage
            continue
        if n not in out:
            out.append(n)
    if not out or len(out) > MAX_SIZES:
        return DEFAULT_SIZES
    return tuple(out)


def _num(x):
    """A real finite number (never bool/str/NaN/inf)."""
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


class TickBars:
    def __init__(self, sizes=None, path=None, save_every_s=30.0):
        self.sizes = parse_sizes() if sizes is None else tuple(sizes)
        self._c = {}    # cid -> {"since": ms|None, "done": {N: deque}, "cur": {N: dict}, "gap": bool}
        self._path, self._every, self._last_save = path, save_every_s, time.monotonic()
        if path:
            self._load()

    # ---- persistence ----------------------------------------------------------------------
    @staticmethod
    def _row(b):
        return [b["t"], b["t_end"], b["o"], b["h"], b["l"], b["c"], b["v"], b["buy"], b["sell"], b["unk"], b["n"], 1 if b["gap"] else 0]

    @staticmethod
    def _bar(r, size):
        """File row -> bar dict, or None when anything is off. Trust nothing in the file."""
        def ts(v):
            return isinstance(v, int) and not isinstance(v, bool) and TS_MIN <= v <= TS_MAX
        if not isinstance(r, list) or len(r) != 12 or not ts(r[0]) or not ts(r[1]) or r[1] < r[0]:
            return None
        px, vols = r[2:6], r[6:10]
        if not all(_num(x) and 0 < x <= MAX_PX for x in px) or not all(_num(x) and 0 <= x <= 10**9 for x in vols):
            return None
        n, gap = r[10], r[11]
        if not (isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= size) or gap not in (0, 1) or isinstance(gap, bool):
            return None
        return {"t": r[0], "t_end": r[1], "o": px[0], "h": px[1], "l": px[2], "c": px[3], "v": vols[0],
                "buy": vols[1], "sell": vols[2], "unk": vols[3], "n": n, "gap": bool(gap)}

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError):
            print("ticks: ignoring unreadable ticks file (starting empty)", flush=True)
            return
        try:
            if not isinstance(raw, dict) or raw.get("v") != 1 or not isinstance(raw.get("c"), dict):
                raise ValueError("version")
            loaded = {}
            for cid, c in list(raw["c"].items())[:MAX_CONTRACTS]:
                since = c.get("since")
                if not (isinstance(since, int) and not isinstance(since, bool) and TS_MIN <= since <= TS_MAX):
                    raise ValueError("since")
                rec = {"since": since, "done": {n: deque(maxlen=MAX_BARS) for n in self.sizes}, "cur": {}, "gap": True}   # not listening while the desk was down
                for n_s, rows in (c.get("done") or {}).items():
                    n = int(n_s)
                    if not isinstance(rows, list) or len(rows) > MAX_BARS:
                        raise ValueError("rows")
                    bars = [self._bar(r, n) for r in rows]
                    if any(b is None for b in bars) or any(a["t"] >= b["t"] for a, b in zip(bars, bars[1:])):
                        raise ValueError("bar")
                    if n in rec["done"]:
                        rec["done"][n].extend(bars)
                for n_s, r in (c.get("cur") or {}).items():
                    n = int(n_s)
                    b = self._bar(r, n)
                    if b is None:
                        raise ValueError("cur")
                    if n in rec["done"]:               # the bar that was forming at shutdown is closed SHORT and flagged: it spans the restart
                        b["gap"] = True
                        rec["done"][n].append(b)
                loaded[str(cid)] = rec
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            print("ticks: ignoring malformed ticks file (starting empty)", flush=True)
            return
        self._c = loaded

    def save(self):
        if not self._path:
            return
        try:
            raw = {"v": 1, "c": {cid: {"since": c["since"],
                                       "done": {str(n): [self._row(b) for b in d] for n, d in c["done"].items()},
                                       "cur": {str(n): self._row(b) for n, b in c["cur"].items()}}
                                 for cid, c in self._c.items() if c["since"] is not None}}
            tmp = f"{self._path}.tmp"
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            os.replace(tmp, self._path)
        except OSError:
            pass                  # never let a full disk stop the tape
        self._last_save = time.monotonic()

    def _cid(self, cid):
        if cid not in self._c:
            if len(self._c) >= MAX_CONTRACTS:
                self._c.pop(next(iter(self._c)))
            self._c[cid] = {"since": None, "done": {n: deque(maxlen=MAX_BARS) for n in self.sizes},
                            "cur": {}, "gap": False}
        return self._c[cid]

    def mark_gap(self, cid=None):
        """Prints may have been lost: flag every forming bar, and the next bar if none is forming."""
        for key in ([cid] if cid is not None else list(self._c)):
            c = self._c.get(key)
            if c is None or c["since"] is None:
                continue
            c["gap"] = True
            for b in c["cur"].values():
                b["gap"] = True

    def add(self, cid, ts_ms, price, vol, side):
        """One print. Anything not a finite positive number marks a gap and is NOT counted."""
        if (not _num(ts_ms) or ts_ms <= 0 or not _num(price) or price <= 0
                or not _num(vol) or not 0 < vol <= MAX_TRADE_VOL):
            self.mark_gap(cid)
            return
        c = self._cid(cid)
        t = int(ts_ms)
        if c["since"] is None:
            c["since"] = t
        vol = int(vol) if float(vol).is_integer() else float(vol)
        pending, c["gap"] = c["gap"], False
        for n in self.sizes:
            b = c["cur"].get(n)
            if b is None:
                b = c["cur"][n] = {"t": t, "t_end": t, "o": price, "h": price, "l": price, "c": price,
                                   "v": 0, "buy": 0, "sell": 0, "unk": 0, "n": 0, "gap": False}
            if pending:
                b["gap"] = True
            b["t_end"] = t
            b["h"] = max(b["h"], price)
            b["l"] = min(b["l"], price)
            b["c"] = price
            b["v"] += vol
            b["buy" if side == 0 else "sell" if side == 1 else "unk"] += vol
            b["n"] += 1
            if b["n"] >= n:
                c["done"][n].append(b)
                del c["cur"][n]
        if self._path and time.monotonic() - self._last_save >= self._every:
            self.save()

    def add_trade(self, cid, trade, ts_fn):
        """Feed one raw GatewayTrade dict. Never raises: anything malformed marks a gap instead."""
        try:
            self.add(cid, ts_fn(trade["timestamp"]), trade.get("price"), trade.get("volume"), trade.get("type"))
        except Exception:  # noqa: BLE001
            self.mark_gap(cid)

    def snapshot(self, cid, n, limit=500):
        """{"v":1,"n":N,"since":ms|None,"bars":[[t,o,h,l,c,v,buy,sell,gap,n], ...],"te":[last print ms per bar]} oldest first,
        forming bar last. `te` is additive (the UI ignores it); the recorder merge needs each bar's time span."""
        c = self._c.get(cid)
        if n not in self.sizes or not c:
            return {"v": 1, "n": n if n in self.sizes else None, "since": None, "bars": []}
        bars = list(c["done"][n])
        if n in c["cur"]:
            bars.append(c["cur"][n])
        try:
            limit = max(1, min(int(limit), MAX_BARS + 1))
        except (TypeError, ValueError, OverflowError):
            limit = 500
        rows = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"], b["buy"], b["sell"], 1 if b["gap"] else 0, b["n"]]
                for b in bars[-limit:]]
        return {"v": 1, "n": n, "since": c["since"], "bars": rows, "te": [b["t_end"] for b in bars[-limit:]]}
