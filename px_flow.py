"""Real buy/sell volume from the ProjectX trade tape (GatewayTrade `type`: 0 = buyer aggressed, 1 = seller aggressed).

Pure, no I/O. Trades are summed into 1-minute buckets per contract. ProjectX has no historical tick endpoint, so
this only covers time the desk was subscribed: `since` says where coverage starts, and any minute that touched a
hub reconnect or a malformed trade is flagged `partial` and never drawn as if it were complete. Volume of unknown
side is counted separately (`u`), never guessed into buy or sell. Numbers only; nothing from the gateway is echoed.
"""
import json
import math
import os
import time

MIN_MS = 60_000
MAX_MINUTES = 3000          # about two trading days per contract
MAX_CONTRACTS = 8
MAX_TRADE_VOL = 100_000
MAX_HOLES = 200
TS_MIN, TS_MAX = 946_684_800_000, 4_102_444_800_000      # 2000-01-01 .. 2100-01-01 in ms: anything else is not a real trade time


class FlowStore:
    """`holes` are [from_minute, to_minute] stretches where trades were NOT being received (reconnect, restart, closed
    subscription); a bar that overlaps one is never drawn. With `path` the buckets survive a restart: the file is
    rewritten every `save_every_s` while trades flow, and on load every contract starts flagged so the downtime
    becomes a hole instead of a quiet-looking stretch of zeros."""

    def __init__(self, path=None, save_every_s=30.0):
        self._c = {}        # cid -> {"since", "m": {minute_ms: [buy, sell, unknown]}, "partial": set, "gap": bool, "holes": [], "last_ts"}
        self._path, self._every, self._last_save = path, save_every_s, time.monotonic()
        if path:
            self._load()

    def _cid(self, cid):
        if cid not in self._c:
            if len(self._c) >= MAX_CONTRACTS:
                self._c.pop(next(iter(self._c)))
            self._c[cid] = {"since": None, "m": {}, "partial": set(), "gap": False, "holes": [], "last_ts": None}
        return self._c[cid]

    # ---- persistence ----------------------------------------------------------------------
    def _load(self):
        """Trust nothing in the file: types, ranges and sizes are all re-checked, and anything off starts clean."""
        def okts(v):
            return isinstance(v, int) and not isinstance(v, bool) and TS_MIN <= v <= TS_MAX
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for cid, c in list((raw.get("c") or {}).items())[:MAX_CONTRACTS]:
                m = {}
                for k, v in (c.get("m") or {}).items():
                    k = int(k)
                    if okts(k) and isinstance(v, list) and len(v) >= 3 and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 10**9 for x in v[:3]):
                        m[k] = [int(x) for x in v[:3]]
                keep = sorted(m)[-MAX_MINUTES:]
                m = {k: m[k] for k in keep}
                since, last = c.get("since"), c.get("last_ts")
                if not (okts(since) and okts(last)) or not m:
                    continue                                   # a malformed contract record is dropped, not repaired
                holes = [[int(a), int(b)] for a, b in (c.get("holes") or [])[-MAX_HOLES:] if okts(a) and okts(b)]
                partial = {int(x) for x in (c.get("partial") or [])[-MAX_MINUTES:] if okts(x)}
                self._c[str(cid)] = {"since": since, "m": m, "partial": partial, "gap": True,   # not listening while the desk was down
                                     "holes": holes, "last_ts": last}
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            self._c = {}          # unreadable or wrongly-shaped file: start clean rather than trust it

    def save(self):
        if not self._path:
            return
        try:
            raw = {"v": 1, "c": {cid: {"since": c["since"], "m": {str(k): v for k, v in c["m"].items()}, "partial": sorted(c["partial"]),
                                       "holes": c["holes"], "last_ts": c["last_ts"]} for cid, c in self._c.items()}}
            tmp = f"{self._path}.tmp"
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            os.replace(tmp, self._path)
        except OSError:
            pass                  # never let a full disk stop the tape
        self._last_save = time.monotonic()

    def add(self, cid, ts_ms, vol, side):
        """One trade. A bad number marks its minute partial instead of being silently dropped."""
        if isinstance(ts_ms, bool) or not isinstance(ts_ms, (int, float)) or not math.isfinite(ts_ms) or not TS_MIN <= ts_ms <= TS_MAX:
            self.mark_gap(cid)                  # an unusable timestamp means a trade we cannot place: its minute is unknown
            return
        c = self._cid(cid)
        k = int(ts_ms) // MIN_MS * MIN_MS
        if c["since"] is None:
            c["since"] = k + MIN_MS            # the first minute is entered mid-way: coverage starts at the next one
            c["partial"].add(k)
        if c["gap"]:
            c["gap"] = False
            c["partial"].add(k)                # trades were lost before this one
            if c["last_ts"] is not None and k > c["last_ts"] // MIN_MS * MIN_MS:
                c["partial"].add(c["last_ts"] // MIN_MS * MIN_MS)
                c["holes"] = (c["holes"] + [[c["last_ts"] // MIN_MS * MIN_MS, k]])[-MAX_HOLES:]
        c["last_ts"] = int(ts_ms) if c["last_ts"] is None else max(c["last_ts"], int(ts_ms))
        if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not math.isfinite(vol) or not 0 < vol <= MAX_TRADE_VOL:
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
            del c["m"][old]
            c["partial"].discard(old)
            c["since"] = max(c["since"], old + 2 * MIN_MS)
            c["holes"] = [h for h in c["holes"] if h[1] > old]
        if self._path and time.monotonic() - self._last_save >= self._every:
            self.save()

    def mark_gap(self, cid=None, now_ms=None):
        """A hub reconnect/resync: trades may have been lost. The minute it happened in and the next one are partial."""
        for key in ([cid] if cid is not None else list(self._c)):
            c = self._c.get(key)
            if c is None or c["since"] is None:
                continue
            c["gap"] = True
            if now_ms is not None:
                c["partial"].add(int(now_ms) // MIN_MS * MIN_MS)

    def snapshot(self, cid, now_ms=None):
        """`holes` = the stored closed holes plus, while a gap is flagged and no trade has arrived since, one derived OPEN
        hole [last trade minute, now minute]: an outage that is still going on must not age into a quiet-looking window."""
        c = self._c.get(cid)
        if not c or c["since"] is None:
            return {"v": 1, "since": None, "minutes": [], "holes": []}
        rows = [[k, b[0], b[1], b[2], 1 if k in c["partial"] else 0] for k, b in sorted(c["m"].items())]
        holes = [list(h) for h in c["holes"]]
        if c["gap"] and c["last_ts"] is not None:
            if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms):
                now_ms = time.time() * 1000
            lo = c["last_ts"] // MIN_MS * MIN_MS
            holes.append([lo, max(lo, int(now_ms) // MIN_MS * MIN_MS)])      # derived, never stored
        return {"v": 1, "since": c["since"], "minutes": rows, "holes": holes}     # row = [minute_ms, buy, sell, unknown, partial]


WINDOWS_MIN = (5, 15, 60)


def summary(store, cid, now_ms):
    """Trailing windows for the AI surface: {since, w: {minutes: {buy, sell, unknown, delta, complete}}}. A window is
    `complete` only if coverage started before it and no minute in it is partial. Numbers only."""
    if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms):
        now_ms = time.time() * 1000
    snap = store.snapshot(cid, now_ms)
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
