"""Paper trading journal: pure rules for opening and grading hypothetical trades from the desk's own grades and the analyst callouts.

Nothing here touches an account: no client, no guard, no hub, no I/O. `scripts/paper_journal.py` feeds it rows and bars and appends the result
to `data/paper_trades.jsonl`. Same conventions as the shadow map record and the bias scorer, so the numbers can sit side by side:
  - a bias trade enters at the closed bar's close, stops 1.5 ATR away (rounded UP to a tick) and targets 1.5 R;
  - a callout trade uses the analyst's own entry, stop and first target, and only counts once price trades through the entry;
  - a bar that touches both stop and target counts as the stop;
  - everything is flat at the 15:08 CT deadline, or after `HORIZON_BARS` five-minute bars, whichever comes first;
  - the opposite side with the same stop distance is the baseline (`r_opp`), so a streak of winners is never read without its coin flip;
  - one open paper trade per contract, like the real guard's one protection owner;
  - no opens in the no-new-entries window (14:55 to 17:00 CT) or while the market is closed for the weekend.
Display and logging only. It never produces an order, a ticket or a stage request.
"""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
BAR_MS = 300_000
STOP_ATR = 1.5
TARGET_R = 1.5
HORIZON_BARS = 24
FLAT_BY = (15, 8)
NO_NEW_FROM = (14, 55)
SESSION_OPEN = (17, 0)
GOOD_GRADES = ("A", "B")
CALLOUT_WAIT_BARS = 6          # a callout entry that price never trades through inside this many bars was missed
MIN_N = 30                     # no rate under this many graded trades (the desk's standing rule)


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _tick_round(x, tick, up=False):
    n = x / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 8)


def _ct(ms):
    return datetime.fromtimestamp(ms / 1000, CT)


def market_open(ms):
    """False for the weekend break (Fri 16:00 CT to Sun 17:00 CT) and the daily 16:00 to 17:00 CT maintenance hour. Holidays are not modelled."""
    d = _ct(ms)
    hm, wd = (d.hour, d.minute), d.weekday()
    if wd == 5 or (wd == 4 and hm >= (16, 0)) or (wd == 6 and hm < SESSION_OPEN):
        return False
    return not ((16, 0) <= hm < SESSION_OPEN)


def can_open(ms):
    """Mirror of the guard's windows: not in the no-new-entries window (14:55 to 17:00 CT) and not while the market is closed."""
    d = _ct(ms)
    return market_open(ms) and not (NO_NEW_FROM <= (d.hour, d.minute) < SESSION_OPEN)


def flat_by_ms(ms):
    """The next 15:08 CT at or after `ms` (the deadline of the trading day the trade lives in)."""
    d = _ct(ms)
    t = d.replace(hour=FLAT_BY[0], minute=FLAT_BY[1], second=0, microsecond=0)
    if d >= t:
        t += timedelta(days=1)
    while t.weekday() >= 5:
        t += timedelta(days=1)
    return int(t.timestamp() * 1000)


def from_bias(row, tick, open_contracts=()):
    """A paper trade from one bias-log row, or None. Only A or B leans with a usable price and ATR, inside the entry windows, and only
    when that contract has no open paper trade."""
    if not isinstance(row, dict) or row.get("grade") not in GOOD_GRADES or row.get("direction") not in ("LONG", "SHORT"):
        return None
    cid, ts, px, atr = row.get("contract"), row.get("bar_ts"), row.get("price"), row.get("atr")
    if not isinstance(cid, str) or cid in open_contracts or not (_fin(ts) and _fin(px) and _fin(atr) and atr > 0 and _fin(tick) and tick > 0):
        return None
    if not can_open(int(ts) + BAR_MS):
        return None
    side = row["direction"]
    sgn = 1 if side == "LONG" else -1
    d = _tick_round(STOP_ATR * atr, tick, up=True)
    entry = float(px)
    return {"id": f"bias:{cid}:{int(ts)}", "source": "bias", "contract": cid, "side": side, "t": int(ts) + BAR_MS, "entry": entry,
            "stop": round(entry - sgn * d, 8), "target": round(entry + sgn * TARGET_R * d, 8), "stop_dist": d, "grade": row.get("grade"),
            "score": row.get("score"), "flags": [f for f in (row.get("flags") or []) if isinstance(f, str)][:8], "s21": row.get("s21"),
            "rule": row.get("rule"), "wait_entry": False}


def from_callout(item, contract, tick, now_ms, open_contracts=()):
    """A paper trade from one analyst ENTRY callout (symbol already mapped to a desk contract), or None. The analyst's own entry, stop and first
    target; the trade only counts once price trades through the entry."""
    if not isinstance(item, dict) or item.get("event") != "ENTRY" or not isinstance(contract, str) or contract in open_contracts:
        return None
    e, s, t1, age = item.get("entry"), item.get("stop"), item.get("tp1"), item.get("age_s")
    if not (_fin(e) and _fin(s) and _fin(t1) and _fin(age) and _fin(tick) and tick > 0) or e == s:
        return None
    side = item.get("side") if item.get("side") in ("LONG", "SHORT") else ("SHORT" if s > e else "LONG")
    if (side == "LONG") != (s < e) or (side == "LONG") != (t1 > e):          # a stop or target on the wrong side of entry is a garbled post
        return None
    posted = int(now_ms - age * 1000)
    return {"id": f"callout:{contract}:{posted // 1000}:{e:g}", "source": "callout", "contract": contract, "side": side, "t": posted,
            "entry": float(e), "stop": float(s), "target": float(t1), "stop_dist": round(abs(e - s), 8), "grade": None, "score": None,
            "flags": [], "s21": None, "rule": None, "wait_entry": True}


def _walk(e, s, tg, sgn, d, bars, entered, horizon):
    """One side's result over `bars` (closed, oldest first, all after the trade opened). e/s/tg are entry, stop and target prices, sgn +1 long or -1
    short, d the stop distance. None while undecided."""
    n = 0
    for b in bars:
        n += 1
        if not entered:
            if b["low"] <= e <= b["high"]:
                entered = True
            elif n >= CALLOUT_WAIT_BARS:
                return {"result": "MISSED", "r": None, "exit": None, "bars": n}
            else:
                continue
        hit_stop = b["low"] <= s if sgn > 0 else b["high"] >= s
        hit_tgt = b["high"] >= tg if sgn > 0 else b["low"] <= tg
        if hit_stop:                                             # both in one bar: the stop
            return {"result": "STOP", "r": -1.0, "exit": s, "bars": n}
        if hit_tgt:
            return {"result": "TARGET", "r": round(sgn * (tg - e) / d, 2), "exit": tg, "bars": n}
        if n >= horizon:
            return {"result": "TIME", "r": round(sgn * (b["close"] - e) / d, 2), "exit": b["close"], "bars": n}
    return None


def resolve(trade, bars, now_ms, horizon=HORIZON_BARS):
    """Grade a paper trade against the CLOSED bars after it opened. None until it has an outcome (stop, target, the deadline, the horizon, or a missed
    callout entry). The returned dict carries `r` (this trade) and `r_opp` (a bias trade's opposite side, same stop distance, mirrored target),
    both in multiples of the stop."""
    sgn = 1 if trade["side"] == "LONG" else -1
    e, s, tg, d = trade["entry"], trade["stop"], trade["target"], trade["stop_dist"]
    end = flat_by_ms(trade["t"])
    closed = [b for b in bars if all(_fin(b.get(k)) for k in ("timestamp", "high", "low", "close")) and b["timestamp"] >= trade["t"] and b["timestamp"] + BAR_MS <= now_ms]
    inside = [b for b in closed if b["timestamp"] + BAR_MS <= end]          # a bar that runs past the deadline is not part of the trade
    wait = bool(trade["wait_entry"])
    res = _walk(e, s, tg, sgn, d, inside, not wait, horizon)
    flat_by = res is None and len(closed) > len(inside) and bool(inside)
    if flat_by:                                                           # the deadline passed with no stop or target: flat at the last bar before it
        res = {"result": "FLAT_BY", "r": round(sgn * (inside[-1]["close"] - e) / d, 2), "exit": inside[-1]["close"], "bars": len(inside)}
        if wait and not any(b["low"] <= e <= b["high"] for b in inside):
            res = {"result": "MISSED", "r": None, "exit": None, "bars": len(inside)}
    if res is None:
        return None
    res["r_opp"] = None
    if trade["source"] == "bias":                          # the baseline lives exactly as long as the real trade: if the other side is still undecided when this one closes, it is marked at that bar's close
        span = inside[:res["bars"]]
        opp = _walk(e, round(e + sgn * d, 8), round(e - sgn * TARGET_R * d, 8), -sgn, d, span, True, horizon)
        if opp is not None:
            res["r_opp"] = opp["r"]
        elif span:
            res["r_opp"] = round(-sgn * (span[-1]["close"] - e) / d, 2)
    return res


def stats(closed):
    """Summary over graded paper trades (rows with `r`). MISSED callouts are counted apart and never enter a mean."""
    out = {"n": 0, "missed": 0, "win_pct": None, "mean_r": None, "mean_r_opp": None, "total_r": None, "enough": False}
    graded = [c for c in closed if c.get("result") != "MISSED" and _fin(c.get("r"))]
    out["missed"] = sum(1 for c in closed if c.get("result") == "MISSED")
    out["n"] = len(graded)
    if not graded:
        return out
    out["enough"] = len(graded) >= MIN_N
    out["total_r"] = round(sum(c["r"] for c in graded), 2)
    opp = [c["r_opp"] for c in graded if _fin(c.get("r_opp"))]
    out["mean_r_opp"] = round(sum(opp) / len(opp), 3) if opp else None
    if out["enough"]:                                              # no rate on a handful of trades: it is noise
        out["win_pct"] = round(100.0 * sum(1 for c in graded if c["r"] > 0) / len(graded), 1)
        out["mean_r"] = round(out["total_r"] / len(graded), 3)
    return out


# ---- journal bookkeeping (still pure: rows in, rows out) --------------------------------------------------------------

MAX_OPEN_AGE_MS = 12 * 60_000          # a grade older than this is not opened late: a paper trade is taken when the signal is fresh, never backfilled
MAX_CALLOUT_AGE_MS = 30 * 60_000
TO_MICRO = {"NQ": "MNQ", "ES": "MES", "GC": "MGC", "CL": "MCL", "RTY": "M2K", "YM": "MYM"}


def state(journal):
    """(open trades, ids ever opened) from journal rows ({kind_row: open|close, ...}). An open row without a close row is still open."""
    opened, closed = {}, set()
    for r in journal:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            continue
        if r.get("kind_row") == "open":
            opened[r["id"]] = r
        elif r.get("kind_row") == "close":
            closed.add(r["id"])
    return [t for i, t in opened.items() if i not in closed], set(opened)


def open_new(journal, bias_rows, callouts, contract_for_symbol, tick_for, now_ms):
    """New `open` journal rows for fresh B-or-better bias leans and fresh analyst ENTRY callouts. Idempotent: an id that was ever opened is never
    opened again, and a contract with an open paper trade takes no second one."""
    open_tr, seen = state(journal)
    busy = {t["contract"] for t in open_tr}
    rows = []
    for r in sorted((r for r in bias_rows if isinstance(r, dict) and _fin(r.get("bar_ts"))), key=lambda r: r["bar_ts"]):
        if now_ms - (r["bar_ts"] + BAR_MS) > MAX_OPEN_AGE_MS or r["bar_ts"] + BAR_MS > now_ms + 60_000:
            continue
        tick = tick_for(r.get("contract"))
        t = from_bias(r, tick, busy)
        if t and t["id"] not in seen:
            seen.add(t["id"]); busy.add(t["contract"]); rows.append({"kind_row": "open", **t, "opened_at": int(now_ms)})
    for it in callouts or []:
        if not isinstance(it, dict) or not isinstance(it.get("symbol"), str):
            continue
        cid = contract_for_symbol(TO_MICRO.get(it["symbol"], it["symbol"]))
        if not cid or not _fin(it.get("age_s")) or it["age_s"] * 1000 > MAX_CALLOUT_AGE_MS:
            continue
        t = from_callout(it, cid, tick_for(cid), now_ms, busy)
        if t and t["id"] not in seen:
            seen.add(t["id"]); busy.add(t["contract"]); rows.append({"kind_row": "open", **t, "opened_at": int(now_ms)})
    return rows


def close_due(journal, bars_for, now_ms):
    """New `close` rows for open trades that have an outcome. bars_for(contract) -> closed 5m bars (or None when they could not be read)."""
    open_tr, _ = state(journal)
    rows, cache = [], {}
    for t in open_tr:
        if t["contract"] not in cache:
            cache[t["contract"]] = bars_for(t["contract"])
        bars = cache[t["contract"]]
        if not bars:
            continue
        res = resolve(t, bars, now_ms)
        if res is not None:
            rows.append({"kind_row": "close", "id": t["id"], "source": t["source"], "contract": t["contract"], "side": t["side"], "grade": t.get("grade"),
                         "s21": t.get("s21"), "flags": t.get("flags") or [], "entry": t["entry"], "closed_at": int(now_ms), **res})
    return rows


def report(journal):
    """Plain-text summary: overall, by source, by grade. Withholds a rate under MIN_N graded trades."""
    open_tr, _ = state(journal)
    closes = [r for r in journal if isinstance(r, dict) and r.get("kind_row") == "close"]
    lines = [f"PAPER JOURNAL: {len(closes)} closed, {len(open_tr)} open (no orders, no account: hypothetical fills at the bar close or the analyst's entry)"]

    def line(label, rows):
        s = stats(rows)
        if not s["n"] and not s["missed"]:
            return None
        if s["enough"]:
            return f"  {label:<16} n={s['n']:<4} win={s['win_pct']}%  mean={s['mean_r']:+.2f}R  vs opposite side {s['mean_r_opp'] if s['mean_r_opp'] is not None else 'NA'}R  total={s['total_r']:+.2f}R  missed={s['missed']}"
        return f"  {label:<16} n={s['n']:<4} (under {MIN_N}: no rate)  total={s['total_r'] if s['total_r'] is not None else 0:+.2f}R  missed={s['missed']}"
    for label, rows in [("ALL", closes)] + [(f"source {k}", [c for c in closes if c.get("source") == k]) for k in ("bias", "callout")] \
            + [(f"grade {g}", [c for c in closes if c.get("grade") == g]) for g in GOOD_GRADES] \
            + [("EMA_TURN flagged", [c for c in closes if "EMA_TURN" in (c.get("flags") or [])]), ("EMA21 slope with", [c for c in closes if c.get("source") == "bias" and c.get("s21") in ("UP", "DOWN") and "EMA_TURN" not in (c.get("flags") or [])])]:
        ln = line(label, rows)
        if ln:
            lines.append(ln)
    for c in closes[-8:]:
        r = c.get("r")
        lines.append(f"  {c.get('source', '?'):<7} {str(c.get('contract', '')).split('.')[3] if str(c.get('contract', '')).count('.') >= 3 else '?':<4} {c.get('side', '?'):<5} {c.get('grade') or '-':<2} {c.get('result', '?'):<7} {('%+.2fR' % r) if isinstance(r, (int, float)) else 'missed'}")
    for t in open_tr:
        lines.append(f"  OPEN {t['source']} {t['contract'].split('.')[3]} {t['side']} @ {t['entry']} stop {t['stop']} target {t['target']}")
    return "\n".join(lines)
