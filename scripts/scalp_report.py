#!/usr/bin/env python3
"""Scalp report: rebuild the desk agent's analysis memory (data/memory/) from the fill ledger and the desk's own bars.

    scripts/scalp_report.py [--days 14] [--no-charts] [--keep 80]

Writes, atomically: data/memory/scalp.md (slices, fee drag, what was left on the table), trips.md (recent trips with the numbers true at
entry and the chart file), index.md (which file answers what) and data/memory/charts/*.png (what the chart looked like around each trip).
trades.md and notes.md are the desk's and the trader's; this script never touches them.

Read-only and standalone. Bars come from the RUNNING desk's loopback GET /bars route (PX_AGENT_DESK, default http://127.0.0.1:8600), never
from a second broker login: a second session logs the desk out and halts it. Only trips not yet in data/scalp_cache.json cost any request,
so a run with nothing new makes none (and a quiet weekend stays quiet). Without Pillow it still writes every number, just no charts.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from projectx_desk import journal, scalp, tripchart  # noqa: E402

DESK = os.environ.get("PX_AGENT_DESK", "http://127.0.0.1:8600").rstrip("/")
CACHE_V = 1
WARMUP_1M_MIN, WARMUP_5M_MIN, POST_MIN = 120, 240, 15
GAP_MIN, MAX_BARS = 180, 1000
PAUSE_S = 0.8                      # the broker allows 50 bar reads / 30 s; the desk's own chart polling shares that budget


def paths(root):
    root = Path(root)
    mem = Path(os.getenv("PX_MEMORY_DIR") or root / "data" / "memory")
    return {"ledger": Path(os.getenv("PX_JOURNAL_FILE") or root / "data" / "trades.jsonl"),
            "bias": Path(os.getenv("PX_BIAS_LOG_FILE") or root / "data" / "bias_log.jsonl"),
            "cache": root / "data" / "scalp_cache.json", "mem": mem, "charts": mem / "charts"}


def cache_key(t):
    return f"{t['contract']}:{t['entry_ts']}:{t['exit_ts']}"


def load_cache(p):
    try:
        c = json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {"v": CACHE_V, "trips": {}}
    return c if isinstance(c, dict) and c.get("v") == CACHE_V and isinstance(c.get("trips"), dict) else {"v": CACHE_V, "trips": {}}


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def clusters(trips):
    """Group trips per contract into runs of nearby activity so one pair of bar reads covers many trips. A new run starts after a
    long gap or when the window would need more than MAX_BARS one-minute bars."""
    out = []
    for c in sorted({t["contract"] for t in trips}):
        cur = []
        for t in sorted((x for x in trips if x["contract"] == c), key=lambda x: x["entry_ts"]):
            if cur and (t["entry_ts"] - cur[-1]["exit_ts"] > GAP_MIN * 60_000
                        or (t["exit_ts"] - cur[0]["entry_ts"]) / 60_000 + WARMUP_1M_MIN + POST_MIN > MAX_BARS - 5):
                out.append(cur)
                cur = []
            cur.append(t)
        if cur:
            out.append(cur)
    return out


class Bars:
    def __init__(self, client):
        self.client = client

    def get(self, contract, minutes_per_bar, end_ms, count):
        for attempt in (0, 1):
            r = self.client.get(f"{DESK}/bars", params={"contract": contract, "unit": 2, "n": minutes_per_bar, "limit": min(MAX_BARS, count), "end": end_ms})
            if r.status_code == 429 and attempt == 0:
                time.sleep(min(30.0, float(r.headers.get("Retry-After") or 5)))
                continue
            r.raise_for_status()
            time.sleep(PAUSE_S)
            rows = r.json()
            return rows if isinstance(rows, list) else []
        return []


def analyse(cluster, bars):
    """(1m, 5m) bars covering a cluster, fetched once."""
    end = cluster[-1]["exit_ts"] + POST_MIN * 60_000
    span_min = (cluster[-1]["exit_ts"] - cluster[0]["entry_ts"]) / 60_000
    one = bars.get(cluster[0]["contract"], 1, end, int(span_min + WARMUP_1M_MIN + POST_MIN) + 5)
    five = bars.get(cluster[0]["contract"], 5, end, int((span_min + WARMUP_5M_MIN + POST_MIN) / 5) + 5)
    return one, five


def build(root, days, charts, keep, client=None):
    p = paths(root)
    ledger = journal.Ledger(p["ledger"])
    trips = journal.attach_context(journal.round_trips(ledger.rows), journal.read_bias(p["bias"]))
    closed = [t for t in trips if not t["open"]]
    cache = load_cache(p["cache"])
    horizon = datetime.now(timezone.utc).timestamp() * 1000 - days * 86_400_000
    todo = [t for t in closed if t["entry_ts"] >= horizon and cache_key(t) not in cache["trips"]]
    status = {"trips": len(closed), "new": len(todo), "fetched": 0, "charts": 0, "error": None}
    if todo:
        if charts and tripchart.available():
            p["charts"].mkdir(parents=True, exist_ok=True)
        own = client is None
        client = client or httpx.Client(timeout=30.0)
        try:
            bars = Bars(client)
            for cl in clusters(todo):
                one, five = analyse(cl, bars)
                status["fetched"] += 2
                for t in cl:
                    x = scalp.entry_features(t, one, five)
                    x["exc"] = scalp.excursion(t, one)
                    x["chart"] = None
                    sub = "BIAS " + scalp.bias_label(t)
                    if x["ext5"] is not None:
                        sub += f"   entry {x['ext5']:+.2f} ATR vs 5m EMA21"
                    if x["exc"]:
                        sub += f"   MFE +{x['exc']['mfe']} / MAE -{x['exc']['mae']} pts"
                    name = scalp.trip_key(t) + ".png"
                    if charts and tripchart.render_png(t, one, five, p["charts"] / name, sub):
                        x["chart"], status["charts"] = name, status["charts"] + 1
                    cache["trips"][cache_key(t)] = x
        except (httpx.HTTPError, ValueError, OSError) as e:        # the desk down or a bad payload: keep what is cached, say so
            status["error"] = type(e).__name__
        finally:
            if own:
                client.close()
    for t in closed:
        t["x"] = cache["trips"].get(cache_key(t))
    if todo:
        atomic_text(p["cache"], json.dumps(cache, separators=(",", ":")))
    keepers = {t["x"]["chart"] for t in sorted(closed, key=lambda t: t["exit_ts"], reverse=True)[:keep] if t.get("x") and t["x"].get("chart")}
    if p["charts"].is_dir():
        for f in p["charts"].glob("*.png"):
            if f.name not in keepers:
                f.unlink()
    now = datetime.now(timezone.utc)
    atomic_text(p["mem"] / "scalp.md", scalp.render_scalp(trips, now))
    atomic_text(p["mem"] / "trips.md", scalp.render_trips(trips))
    atomic_text(p["mem"] / "index.md", scalp.render_index())
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="only fetch bars for trips this recent (older ones keep whatever is cached)")
    ap.add_argument("--keep", type=int, default=80, help="newest trips whose chart PNG is kept")
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args()
    s = build(a.root, a.days, not a.no_charts, a.keep)
    print("scalp_report: " + " ".join(f"{k}={v}" for k, v in s.items()))
    return 0 if not s["error"] else 1


if __name__ == "__main__":
    sys.exit(main())
