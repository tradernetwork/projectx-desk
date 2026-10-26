"""Shadow-mode record of dealer-map event proposals (see mapplan.py for the rules and the three-stage process). Append-only JSON lines in
data/map_proposals.jsonl (PX_MAPPLAN_FILE): `proposal` rows (what would have been proposed, written when the trigger bar closed) and `outcome` rows
(what price then did, graded by mapplan.resolve). Numbers and enums only. Read-only: no route, no state key, no digest line, nothing the trader sees
and nothing that can stage; it never touches the guard, sizing or the order path."""
import json
import os
import pathlib
from datetime import datetime, timedelta

from projectx_desk import levels as levels_mod
from projectx_desk import mapplan as MP
from projectx_desk import sizing
from projectx_desk import tomorrow_feed as F
from projectx_desk.tradeday import CT

MAX_LINES = 20000
FRESH_BAR_S = 150            # the trigger bar closed no more than this long ago: never file an old event after a restart or a gap
MAX_MAP_AGE_S = 6 * 3600
PENDING_WINDOW_S = 8 * 3600


def path_from_env(root):
    return pathlib.Path(os.getenv("PX_MAPPLAN_FILE") or (pathlib.Path(root) / "data" / "map_proposals.jsonl"))


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
        if isinstance(r, dict) and r.get("kind_row") in ("proposal", "outcome"):
            out.append(r)
    return out


def append(path, entry):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        print(json.dumps(entry, separators=(",", ":"), allow_nan=False), file=f)


def ct_date(ms):
    return datetime.fromtimestamp(ms / 1000, CT).date().isoformat()


def next_flat_ms(bar_ts_ms):
    """The first 15:08 CT strictly after the trigger bar: the desk flattens there, so nothing is graded past it."""
    d = datetime.fromtimestamp(bar_ts_ms / 1000, CT)
    t = d.replace(hour=MP.FLAT_BY[0], minute=MP.FLAT_BY[1], second=0, microsecond=0)
    if t <= d:
        t += timedelta(days=1)
    return t.timestamp() * 1000


async def closed_5m(app, cid):
    from projectx_desk import digest
    return await digest._tf_bars(app, cid, "5m")


async def tick(app, path, now_ts, tracked=None, bars_fn=closed_5m):
    """One pass at a 5m bar close: grade proposals that can be graded, then file new ones. Shadow only."""
    now_ms = now_ts * 1000
    tracked = tracked if tracked is not None else (getattr(app.state, "tracked", None) or {})
    rows = load(path)
    for root, cid in tracked.items():
        if not cid:
            continue
        try:
            bars = [b for b in await bars_fn(app, cid) if all(isinstance(b.get(k), (int, float)) for k in ("timestamp", "high", "low", "close"))]
        except Exception:  # noqa: BLE001
            continue
        if len(bars) < 20:
            continue
        done = {r["id"] for r in rows if r["kind_row"] == "outcome"}
        for p in [r for r in rows if r["kind_row"] == "proposal" and r["root"] == root and r["id"] not in done and now_ms - r["bar_ts"] <= PENDING_WINDOW_S * 1000]:
            after = [b for b in bars if b["timestamp"] > p["bar_ts"]]
            fb = next_flat_ms(p["bar_ts"])
            o = MP.resolve(p, after, flat_by_ms=lambda ts, fb=fb: fb)
            if o:
                append(path, {"kind_row": "outcome", **o, "resolved_at": int(now_ts)})
        rows = load(path)
        res = await F.tomorrow_result(app, cid, now=now_ts)
        if res.get("status") != "OK" or not isinstance(res.get("map"), dict):
            continue
        snap = res.get("snapshot_epoch")
        if not isinstance(snap, (int, float)) or ct_date(snap * 1000) != ct_date(now_ms) or (res.get("age_s") or 0) > MAX_MAP_AGE_S:
            continue                                                       # a map is used only on the day it was made
        last = bars[-1]
        if not (0 <= now_ms - (last["timestamp"] + MP.BAR_MS) <= FRESH_BAR_S * 1000):
            continue
        try:
            atr = sizing.atr([b["high"] for b in bars[-60:]], [b["low"] for b in bars[-60:]], [b["close"] for b in bars[-60:]], 14)
            tick_size = sizing.spec_for(cid).tick_size
        except Exception:  # noqa: BLE001
            continue
        ema = [x for x in levels_mod.ema_series([b["close"] for b in bars], 21) if x is not None]
        extra = [{"price": ema[-1], "role": "EMA21"}] if ema else []
        for p in MP.detect(res["map"], bars[-(MP.LOOKBACK_RECLAIM + 3):], atr, tick_size, root=root, contract=cid, snapshot_epoch=snap, extra_levels=extra):
            if MP.allowed(p, rows, now_ts, ct_date):
                append(path, {"kind_row": "proposal", **p, "made_at": int(now_ts), "map_version": res["map"].get("version")})
                rows = load(path)
