"""Tick bars (N consecutive trade PRINTS per bar) built from the ProjectX trade tape.

Pure, no I/O. retrieveBars has no tick unit and the gateway has no tick history, so bars exist only for time the desk
was subscribed (`since`). Same honesty rules as px_flow: nothing is invented or interpolated. A hub reconnect or a
malformed print flags the bar it landed in `gap` (it may be missing prints) and the print itself is not counted.
Volume of unknown aggressor side (`type` not 0/1) is kept apart (`unk`), never guessed. Numbers only.
"""
import math
import os
from collections import deque

DEFAULT_SIZES = (1024, 2048, 4096)
MIN_N, MAX_N, MAX_SIZES = 50, 50_000, 4
MAX_BARS = 1500             # completed bars kept per contract per N
MAX_CONTRACTS = 8
MAX_TRADE_VOL = 100_000


def parse_sizes(raw=None):
    """`PX_TICK_BARS` -> tuple of distinct ints clamped to MIN_N..MAX_N, at most MAX_SIZES. Garbage entries are skipped;
    nothing usable (or more than MAX_SIZES distinct values) falls back to the default."""
    if raw is None:
        raw = os.environ.get("PX_TICK_BARS", "")
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part.lstrip("-").isdigit():
            continue
        n = min(MAX_N, max(MIN_N, int(part)))
        if n not in out:
            out.append(n)
    if not out or len(out) > MAX_SIZES:
        return DEFAULT_SIZES
    return tuple(out)


def _num(x):
    """A real finite number (never bool/str/NaN/inf)."""
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


class TickBars:
    def __init__(self, sizes=None):
        self.sizes = parse_sizes() if sizes is None else tuple(sizes)
        self._c = {}    # cid -> {"since": ms|None, "done": {N: deque}, "cur": {N: dict}, "gap": bool}

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

    def add_trade(self, cid, trade, ts_fn):
        """Feed one raw GatewayTrade dict. Never raises: anything malformed marks a gap instead."""
        try:
            self.add(cid, ts_fn(trade["timestamp"]), trade.get("price"), trade.get("volume"), trade.get("type"))
        except Exception:  # noqa: BLE001
            self.mark_gap(cid)

    def snapshot(self, cid, n, limit=500):
        """{"v":1,"n":N,"since":ms|None,"bars":[[t,o,h,l,c,v,buy,sell,gap,n], ...]} oldest first, forming bar last."""
        c = self._c.get(cid)
        if n not in self.sizes or not c:
            return {"v": 1, "n": n if n in self.sizes else None, "since": None, "bars": []}
        bars = list(c["done"][n])
        if n in c["cur"]:
            bars.append(c["cur"][n])
        try:
            limit = max(1, min(int(limit), MAX_BARS + 1))
        except (TypeError, ValueError):
            limit = 500
        rows = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"], b["buy"], b["sell"], 1 if b["gap"] else 0, b["n"]]
                for b in bars[-limit:]]
        return {"v": 1, "n": n, "since": c["since"], "bars": rows}
