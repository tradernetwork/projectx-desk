"""Scalp analytics: what the bars say about each closed round trip, and the slices a scalper needs, as plain facts.

PURE: no I/O, no clock, no network. `scripts/scalp_report.py` fetches the bars (through the desk's own read-only /bars route, never a
second broker login) and writes the files; this module only turns trips plus bars into numbers and markdown. Display and logging only:
nothing here reaches the guard, the client or the hubs, and nothing the guard reads comes from here.

Rules carried over from journal.py: a sample size sits beside every figure, no rate is shown under MIN_N, a count under SMALL is
listed but never described. Features use ONLY bars that had closed before the entry (no lookahead).

Excursions (MFE/MAE) come from 1-minute bar ranges, so they are bounds, not tick truth: the bar that holds the entry also holds the
seconds before it. They are flagged `coarse` when the trip is shorter than two minutes.
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from .journal import MIN_N, SMALL
from .levels import atr_series, ema_series
from .regime import ema_slope

CT = ZoneInfo("America/Chicago")
MIN_BARS_1M = 35               # closed 1m bars needed before an entry to read EMA21 and ATR14
MIN_BARS_5M = 30
CHASE_ATR = 1.0                # entry already this many 5m ATRs beyond the 5m EMA21 in the trade's direction
POINT_VALUE = {"MNQ": 2.0, "NQ": 20.0, "MES": 5.0, "ES": 50.0, "M2K": 5.0, "RTY": 50.0, "MYM": 0.5, "YM": 5.0,
               "MGC": 10.0, "GC": 100.0, "MCL": 100.0, "CL": 1000.0}
HOLD_BUCKETS = (("under 1m", 60), ("1-3m", 180), ("3-10m", 600), ("10m+", None))


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def root_of(contract):
    parts = contract.split(".") if isinstance(contract, str) else []
    return parts[3] if len(parts) >= 5 else "?"


def point_value(contract):
    """Dollars per point for one contract, from a fixed table. Unknown root -> None (dollars shown as NA, never guessed)."""
    return POINT_VALUE.get(root_of(contract))


def trip_key(t):
    """Stable file-safe id of a trip: entry time in CT, root, side."""
    d = datetime.fromtimestamp(t["entry_ts"] / 1000, CT)
    return f"{d:%Y%m%d-%H%M%S}-{root_of(t['contract'])}-{t['side']}"


def _dir(t):
    return 1 if t["side"] == "LONG" else -1


def _closed_before(bars, ts_ms, unit_ms):
    return [b for b in bars if b["timestamp"] + unit_ms <= ts_ms]


def excursion(t, bars_1m):
    """Max favourable / adverse move in points while the trip was open, from 1m bar ranges (bounds). None without bars."""
    if t.get("open") or t.get("avg_entry") is None or t.get("avg_exit") is None:
        return None
    win = [b for b in bars_1m if b["timestamp"] + 60_000 > t["entry_ts"] and b["timestamp"] < t["exit_ts"]]
    if not win:
        return None
    d, e = _dir(t), t["avg_entry"]
    fav = max(d * (b["high"] if d > 0 else b["low"]) - d * e for b in win)
    adv = max(d * e - d * (b["low"] if d > 0 else b["high"]) for b in win)
    real = d * (t["avg_exit"] - e)
    mfe, mae = max(0.0, fav, real), max(0.0, adv, -real)
    return {"mfe": round(mfe, 2), "mae": round(mae, 2), "real": round(real, 2), "bars": len(win), "coarse": (t["hold_s"] or 0) < 120}


def entry_features(t, bars_1m, bars_5m):
    """The chart as it stood at the entry, from bars closed before it. Keys are None where the history was too short."""
    d, e = _dir(t), t["avg_entry"]
    out = {"atr1": None, "atr5": None, "ext1": None, "ext5": None, "trend5": None, "run15": None}
    c1 = _closed_before(bars_1m, t["entry_ts"], 60_000)
    if len(c1) >= MIN_BARS_1M:
        cl = [b["close"] for b in c1]
        a, m = atr_series([b["high"] for b in c1], [b["low"] for b in c1], cl)[-1], ema_series(cl, 21)[-1]
        if a and m is not None:
            out["atr1"], out["ext1"] = round(a, 2), round(d * (e - m) / a, 2)
    c5 = _closed_before(bars_5m, t["entry_ts"], 300_000)
    if len(c5) >= MIN_BARS_5M:
        hi, lo, cl = ([b[k] for b in c5] for k in ("high", "low", "close"))
        a, m = atr_series(hi, lo, cl)[-1], ema_series(cl, 21)[-1]
        if a and m is not None:
            out["atr5"], out["ext5"] = round(a, 2), round(d * (e - m) / a, 2)
            out["run15"] = round(d * (e - cl[-4]) / a, 2) if len(cl) >= 4 else None
        s = ema_slope(hi[-60:], lo[-60:], cl[-60:])
        if s:
            out["trend5"] = "flat" if s == "FLAT" else ("with" if (s == "UP") == (d > 0) else "against")
    return out


# ---- slices ---------------------------------------------------------------------------------------------------------------

def hold_bucket(t):
    for name, top in HOLD_BUCKETS:
        if top is None or (t["hold_s"] or 0) < top:
            return name
    return HOLD_BUCKETS[-1][0]


def _after_loss(trips):
    """Per trip: minutes since the last LOSING exit on the same contract, as a label. Order matters, so it works on the sorted list."""
    last, out = {}, {}
    for t in sorted(trips, key=lambda x: x["entry_ts"]):
        ll = last.get(t["contract"])
        gap = None if ll is None else (t["entry_ts"] - ll) / 60_000
        out[id(t)] = "no loss before" if gap is None else "<10m after a loss" if gap < 10 else "10-30m after a loss" if gap < 30 else "30m+ after a loss"
        if not t["open"] and t["net"] < 0:
            last[t["contract"]] = t["exit_ts"]
    return out


def bias_label(t):
    x = t.get("ctx")
    return "no read" if not x else ("neutral" if x["aligned"] is None else "with BIAS" if x["aligned"] else "AGAINST BIAS")


def _ext_label(t):
    v = (t.get("x") or {}).get("ext5")
    return None if v is None else "pullback (below EMA21)" if v < 0 else "near EMA21 (0-1 ATR)" if v < CHASE_ATR else "extended (1+ ATR)"


def _trend_label(t):
    v = (t.get("x") or {}).get("trend5")
    return None if v is None else {"with": "with 5m slope", "against": "against 5m slope", "flat": "5m slope flat"}[v]


def slice_rows(trips, key, order=None):
    """Group closed trips by key(trip): n, net, average net, wins, losses. Trips whose key is None are left out of the table."""
    g = {}
    for t in trips:
        k = key(t)
        if k is not None:
            g.setdefault(k, []).append(t)
    names = [k for k in (order or []) if k in g] + sorted(k for k in g if k not in (order or []))
    rows = []
    for k in names:
        ts = g[k]
        net = sum(t["net"] for t in ts)
        rows.append({"key": k, "n": len(ts), "net": round(net, 2), "avg": round(net / len(ts), 2),
                     "wins": sum(1 for t in ts if t["net"] > 0), "losses": sum(1 for t in ts if t["net"] < 0)})
    return rows


def breakeven_points(trips):
    """Per contract root: average fees per trip divided by dollars per point and average size = the move a trip must make to pay the fees."""
    by = {}
    for t in trips:
        by.setdefault(root_of(t["contract"]), []).append(t)
    out = {}
    for r, ts in by.items():
        pv = POINT_VALUE.get(r)
        if pv:
            out[r] = {"n": len(ts), "fees": round(sum(t["fees"] for t in ts) / len(ts), 2),
                      "points": round(sum(t["fees"] / (pv * t["qty"]) for t in ts) / len(ts), 2)}
    return out


# ---- markdown -------------------------------------------------------------------------------------------------------------

def _money(v):
    return "NA" if v is None else f"{'-' if v < 0 else '+'}${abs(v):.2f}"


def _dur(sec):
    if sec is None:
        return "NA"
    sec = int(sec)
    return f"{sec // 60}m{sec % 60:02d}s"


def _table(title, rows, note=None):
    out = [f"### {title}"]
    if note:
        out.append(note)
    if not rows:
        return out + ["No trips with this data yet.", ""]
    out += ["| Slice | n | Net | Avg per trip | W / L |", "|---|---|---|---|---|"]
    for r in rows:
        mark = " (too few to describe)" if r["n"] < SMALL else ""
        out.append(f"| {r['key']}{mark} | {r['n']} | {_money(r['net'])} | {_money(r['avg'])} | {r['wins']} / {r['losses']} |")
    return out + [""]


def render_scalp(trips, now):
    """data/memory/scalp.md: the slices. Facts and sample sizes; the reader decides what they mean."""
    c = [t for t in trips if not t["open"]]
    L = ["# Scalp analysis", "",
         f"Generated {now.astimezone(CT):%Y-%m-%d %H:%M} CT from {len(c)} closed round trips. Facts computed from fills and 1m/5m bars, not advice.",
         f"A slice with n under {SMALL} is listed but not described, and no win rate is shown under n={MIN_N} (the desk's standing rule). Most slices "
         "below will be under that bar for weeks: read them as bookkeeping, not as findings.",
         "Features use only bars closed BEFORE the entry. MFE/MAE are 1m bar-range bounds (coarse under 2 minutes held).", ""]
    if not c:
        return "\n".join(L + ["No closed round trips recorded yet.", ""])
    gross, fees = sum(t["pnl"] for t in c), sum(t["fees"] for t in c)
    L += ["## Cost of doing business",
          f"- Gross {_money(gross)}, fees ${fees:.2f}, net {_money(gross - fees)}, n={len(c)}"
          + (f". Fees took {100 * fees / gross:.0f}% of gross." if gross > 0 else ". Gross is not positive, so every fee dollar is on top of a loss."),
          f"- Net per trip {_money((gross - fees) / len(c))}, fees per trip ${fees / len(c):.2f}"]
    for r, b in sorted(breakeven_points(c).items()):
        L.append(f"- {r}: a trip must move {b['points']} points just to pay the ${b['fees']:.2f} average fees (n={b['n']})")
    L.append("")
    al = _after_loss(c)
    L += ["## Slices", ""]
    L += _table("By time held", slice_rows(c, hold_bucket, [b[0] for b in HOLD_BUCKETS]))
    L += _table("By entry hour (CT)", slice_rows(c, lambda t: f"{datetime.fromtimestamp(t['entry_ts'] / 1000, CT):%H}:00"))
    L += _table("By contract", slice_rows(c, lambda t: root_of(t["contract"])))
    L += _table("By side", slice_rows(c, lambda t: t["side"]))
    L += _table("By time since the last losing exit (same contract)", slice_rows(
        c, lambda t: al[id(t)], ["<10m after a loss", "10-30m after a loss", "30m+ after a loss", "no loss before"]))
    L += _table("By the desk's BIAS read at entry", slice_rows(c, bias_label, ["with BIAS", "AGAINST BIAS", "neutral", "no read"]))
    L += _table("By entry location vs the 5m EMA21 (in 5m ATRs, trade direction)", slice_rows(
        c, _ext_label, ["pullback (below EMA21)", "near EMA21 (0-1 ATR)", "extended (1+ ATR)"]),
        "Extended means the entry was already 1+ ATR past the 5m EMA21 in the direction taken (chasing).")
    L += _table("By the 5m EMA21 slope vs the side taken", slice_rows(c, _trend_label, ["with 5m slope", "5m slope flat", "against 5m slope"]))
    ex = [(t, t["x"]["exc"]) for t in c if (t.get("x") or {}).get("exc")]
    L += ["## What the trades left on the table", f"Trips with bar data: {len(ex)} of {len(c)}."]
    if ex:
        wins = [(t, e) for t, e in ex if t["net"] > 0]
        loss = [(t, e) for t, e in ex if t["net"] < 0]
        if wins:
            L.append(f"- Winners (n={len(wins)}): average realized {sum(e['real'] for _, e in wins) / len(wins):+.2f} pts, average best excursion "
                     f"{sum(e['mfe'] for _, e in wins) / len(wins):+.2f} pts; {sum(1 for _, e in wins if e['mfe'] >= 2 * max(e['real'], 0.01))} "
                     "of them had a best excursion at least twice what was banked")
        if loss:
            ahead = [(t, e) for t, e in loss if t["x"].get("atr1") and e["mfe"] >= 0.5 * t["x"]["atr1"]]
            L.append(f"- Losers (n={len(loss)}): average realized {sum(e['real'] for _, e in loss) / len(loss):+.2f} pts, average worst excursion "
                     f"-{sum(e['mae'] for _, e in loss) / len(loss):.2f} pts; {len(ahead)} of them were ahead by half a 1m ATR or more before they turned")
        L.append("")
    srt = sorted(c, key=lambda t: t["net"])
    L += ["## Five worst and five best (chart files are in trips.md)", "| Trip | Held | Net | BIAS | Entry vs 5m EMA21 | 5m slope |", "|---|---|---|---|---|---|"]
    for t in srt[:5] + srt[-5:][::-1]:
        x = t.get("x") or {}
        L.append(f"| {trip_key(t)} | {_dur(t['hold_s'])} | {_money(t['net'])} | {bias_label(t)} | "
                 f"{'NA' if x.get('ext5') is None else format(x['ext5'], '+.2f') + ' ATR'} | {_trend_label(t) or 'NA'} |")
    return "\n".join(L + [""])


def render_trips(trips, last=40, chart_dir="charts"):
    """data/memory/trips.md: one row per recent trip with the numbers that were true at entry and the chart file to Read."""
    c = sorted((t for t in trips if not t["open"]), key=lambda t: t["exit_ts"], reverse=True)[:last]
    L = ["# Trip log with charts", "",
         f"Newest {len(c)} closed trips. Each chart is a PNG under data/memory/{chart_dir}/ (1m candles, EMA21 on 1m and 5m, volume, the entry and exit marked): "
         "Read the file to see the chart as it was. 'Ext5' is the entry's distance from the 5m EMA21 in 5m ATRs in the trade's direction; "
         "MFE/MAE are the best and worst points while open (1m bar bounds).", "",
         "| Trip id | Contract | Side | Qty | Entry | Exit | Held | Net | MFE / MAE pts | Ext5 | 5m slope | BIAS | Chart |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in c:
        x = t.get("x") or {}
        e = x.get("exc")
        ch = f"{chart_dir}/{x['chart']}" if x.get("chart") else "none"
        exc = "NA" if not e else "+%s / -%s%s" % (e["mfe"], e["mae"], " (coarse)" if e["coarse"] else "")
        ext = "NA" if x.get("ext5") is None else format(x["ext5"], "+.2f")
        L.append(f"| {trip_key(t)} | {root_of(t['contract'])} | {t['side']} | {t['qty']} | {t['avg_entry']} | {t['avg_exit']} | {_dur(t['hold_s'])} | "
                 f"{_money(t['net'])} | {exc} | {ext} | {x.get('trend5') or 'NA'} | {bias_label(t)} | {ch} |")
    return "\n".join(L + [""])


def render_index():
    """data/memory/index.md: which file answers which question, so the agent reads one file, not five."""
    return "\n".join([
        "# Memory index", "",
        "Read this first, then only the file the question needs. All of it is computed from the trader's fills and the desk's bars. Facts, not advice.", "",
        "| Question | File |", "|---|---|",
        "| How am I doing overall? Which habits has the ledger counted? What were the last trips? | trades.md |",
        "| Do fees eat the edge? Which hold times, hours, contracts, BIAS reads, entry locations lose? What was left on the table? | scalp.md |",
        "| What did the chart look like on trip X? What were the numbers at that entry? | trips.md, then Read the PNG named in the Chart column |",
        "| What does the trader think about it? | notes.md (hand-written, never edited by code) |", "",
        "Rules of the data: no rate is shown under 30 closed trips, a count under 5 is not a pattern, and nothing here is a signal. "
        "If a figure you need is not in these files, say so; do not recompute it by hand from the raw ledger.", ""])
