"""Trade journal: the desk's memory of what the trader actually did.

Fills are the only input (GatewayUserTrade rows, from the live hub and one REST read). They are reduced to an allowlisted row,
appended to `data/trades.jsonl` (append-only, deduped by fill id) and paired into round trips. Each trip is joined with the
BIAS read that stood at its entry (`data/bias_log.jsonl`, last line at or before the entry, never after) and the lot is rendered
as plain facts into `data/memory/trades.md`, which the read-only desk agent reads. Hand-written reflections live in
`data/memory/notes.md`, which this module never touches.

Facts, never advice: counts, dollars and hold times, with the sample size beside every figure and no rate under MIN_N trips.
Display and logging only. Nothing here reads or reaches the guard, the client or the hubs, and a test pins that.
"""
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .tradeday import parse_ts

ROOT = Path(__file__).resolve().parent.parent
CT = ZoneInfo("America/Chicago")
MIN_N = 30                  # no rate or average-as-pattern under this many closed trips (same bar as the rest of the desk)
SMALL = 5                   # below this a count is listed but never described
REVENGE_S = 600             # an entry within 10 minutes of a losing exit on the same contract
CONTRACT = re.compile(r"^CON\.F\.US\.[A-Z0-9]{1,8}\.[A-Z0-9]{1,8}$")
BIAS_STALE_MS = 10 * 60 * 1000


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def fill_row(d):
    """One allowlisted fill, or None. Side 0 is a buy and 1 a sell; anything else is dropped, never guessed."""
    if not isinstance(d, dict) or d.get("voided"):
        return None
    fid, cid, side = d.get("id"), d.get("contractId"), d.get("side")
    price, size = _num(d.get("price")), _num(d.get("size"))
    if isinstance(fid, bool) or not isinstance(fid, int) or not isinstance(cid, str) or not CONTRACT.match(cid):
        return None
    if side not in (0, 1) or isinstance(side, bool) or price is None or price <= 0 or size is None or size <= 0 or size != int(size) or size > 1000:
        return None
    try:
        ts = parse_ts(d["creationTimestamp"])
    except (KeyError, ValueError, AttributeError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    oid = d.get("orderId")
    return {"id": fid, "ts": int(ts.timestamp() * 1000), "contract": cid, "side": "BUY" if side == 0 else "SELL", "price": price,
            "size": int(size), "pnl": _num(d.get("profitAndLoss")), "fees": max(0.0, _num(d.get("fees")) or 0.0),
            "order": oid if isinstance(oid, int) and not isinstance(oid, bool) else None}


def fill_rows(raw):
    return [r for r in (fill_row(d) for d in raw or []) if r]


class Ledger:
    """Append-only fill file. Reads once, dedupes by fill id, never rewrites a line."""

    def __init__(self, path):
        self.path = Path(path)
        self.rows, self.ids = [], set()
        try:
            for line in self.path.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("id"), int) and r["id"] not in self.ids:
                    self.ids.add(r["id"])
                    self.rows.append(r)
        except OSError:
            pass

    def add(self, rows):
        """Append the rows not seen before; returns them."""
        new = [r for r in rows if r["id"] not in self.ids]
        if not new:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for r in new:
                print(json.dumps(r, separators=(",", ":")), file=f)
                self.ids.add(r["id"])
                self.rows.append(r)
        return new


def round_trips(rows):
    """Pair fills into round trips per contract, oldest first. A fill that crosses flat is split: the part that closes goes to
    the old trip, the rest opens the next. Fees are pro-rated by the share of a fill each trip took. Net = P&L - fees.
    A trip still open at the end has exit fields None and open=True."""
    out, live = [], {}
    for r in sorted(rows, key=lambda x: (x["ts"], x["id"])):
        q = r["size"] if r["side"] == "BUY" else -r["size"]
        c = r["contract"]
        while q:
            t = live.get(c)
            share = lambda n: n / r["size"]           # noqa: E731 - the part of this fill that one trip took
            if t is None:
                t = live[c] = {"contract": c, "side": "LONG" if q > 0 else "SHORT", "entry_ts": r["ts"], "exit_ts": None, "qty": 0, "pos": 0,
                               "_in": 0.0, "_in_n": 0, "_out": 0.0, "_out_n": 0, "pnl": 0.0, "fees": 0.0, "n_entries": 0, "n_exits": 0,
                               "open": True}
            if (q > 0) == (t["side"] == "LONG"):                       # adds to the position
                n = abs(q)
                t["_in"] += r["price"] * n; t["_in_n"] += n; t["pos"] += n; t["qty"] = max(t["qty"], t["pos"])
                t["fees"] += r["fees"] * share(n); t["n_entries"] += 1
                q = 0
            else:                                                       # reduces it
                n = min(abs(q), t["pos"])
                t["_out"] += r["price"] * n; t["_out_n"] += n; t["pos"] -= n
                t["pnl"] += (r["pnl"] or 0.0) * share(n); t["fees"] += r["fees"] * share(n); t["n_exits"] += 1
                q += n if q < 0 else -n
                if t["pos"] == 0:
                    t["exit_ts"], t["open"] = r["ts"], False
                    out.append(_finish(live.pop(c)))
    out.extend(_finish(t) for t in live.values())
    out.sort(key=lambda x: x["entry_ts"])
    return out


def _finish(t):
    t = dict(t)
    t["avg_entry"] = round(t.pop("_in") / t["_in_n"], 4) if t["_in_n"] else None
    t["avg_exit"] = round(t.pop("_out") / t["_out_n"], 4) if t["_out_n"] else None
    t.pop("_in_n"), t.pop("_out_n"), t.pop("pos")
    t["pnl"], t["fees"] = round(t["pnl"], 2), round(t["fees"], 4)
    t["net"] = None if t["open"] else round(t["pnl"] - t["fees"], 2)
    t["hold_s"] = None if t["open"] else max(0, (t["exit_ts"] - t["entry_ts"]) // 1000)
    return t


def read_bias(path):
    """bias_log lines reduced to what a trip needs, grouped by contract, oldest first."""
    by = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return by
    for line in lines:
        try:
            b = json.loads(line)
        except ValueError:
            continue
        if not isinstance(b, dict) or not isinstance(b.get("ts"), int) or not isinstance(b.get("contract"), str):
            continue
        by.setdefault(b["contract"], []).append({"ts": b["ts"], "grade": b.get("grade"), "direction": b.get("direction"),
                                                  "score": _num(b.get("score")), "adx": _num(b.get("adx")), "flags": [f for f in (b.get("flags") or []) if isinstance(f, str)][:6]})
    for v in by.values():
        v.sort(key=lambda x: x["ts"])
    return by


def attach_context(trips, bias):
    """The BIAS read standing at each entry: the last line at or before it, no older than ten minutes, never one after (no lookahead).
    `aligned` is whether that read's direction matched the side taken; None when there was no read or it was neutral."""
    for t in trips:
        best = None
        for b in bias.get(t["contract"], []):
            if b["ts"] > t["entry_ts"]:
                break
            best = b
        if best and t["entry_ts"] - best["ts"] <= BIAS_STALE_MS and best.get("grade") not in (None, "NA"):
            d = best.get("direction")
            t["ctx"] = {"grade": best["grade"], "direction": d, "flags": best["flags"],
                        "aligned": (d == t["side"]) if d in ("LONG", "SHORT") else None}
        else:
            t["ctx"] = None
    return trips


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def summarize(trips):
    """Counts and dollars over CLOSED trips. Rates are None under MIN_N."""
    c = [t for t in trips if not t["open"]]
    wins, losses = [t for t in c if t["net"] > 0], [t for t in c if t["net"] < 0]
    s = {"n": len(c), "wins": len(wins), "losses": len(losses), "scratch": len(c) - len(wins) - len(losses),
         "net": round(sum(t["net"] for t in c), 2), "fees": round(sum(t["fees"] for t in c), 2),
         "avg_win": _avg([t["net"] for t in wins]), "avg_loss": _avg([t["net"] for t in losses]),
         "hold_win_s": _avg([t["hold_s"] for t in wins]), "hold_loss_s": _avg([t["hold_s"] for t in losses])}
    s["win_rate"] = round(100 * len(wins) / len(c), 1) if len(c) >= MIN_N else None
    s["payoff"] = round(abs(s["avg_win"] / s["avg_loss"]), 2) if s["avg_win"] and s["avg_loss"] and len(c) >= MIN_N else None
    revenge, scaled, counter, late, early = [], [], [], [], []
    last_loss = {}
    for t in sorted(c, key=lambda x: x["entry_ts"]):
        ll = last_loss.get(t["contract"])
        if ll is not None and t["entry_ts"] - ll <= REVENGE_S * 1000:
            revenge.append(t)
        if t["net"] < 0:
            last_loss[t["contract"]] = t["exit_ts"]
        if t["n_entries"] > 1:
            scaled.append(t)
        x = t.get("ctx")
        if x and x["aligned"] is False and x["direction"] in ("LONG", "SHORT"):
            counter.append(t)
        h = datetime.fromtimestamp(t["entry_ts"] / 1000, CT)
        if (h.hour, h.minute) >= (14, 55):
            late.append(t)
        if (h.hour, h.minute) < (8, 45) and (h.hour, h.minute) >= (8, 30):
            early.append(t)
    box = lambda ts: {"n": len(ts), "net": round(sum(t["net"] for t in ts), 2)}      # noqa: E731
    s.update(revenge=box(revenge), scaled_in=box(scaled), counter_bias=box(counter), after_1455=box(late), first_15_min=box(early))
    return s


def _money(v):
    return "NA" if v is None else f"{'-' if v < 0 else '+'}${abs(v):.2f}"


def _dur(sec):
    if sec is None:
        return "NA"
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def render_memory(trips, now=None, last=12):
    """The agent's trade memory as markdown. Plain facts with sample sizes; the reader draws conclusions."""
    now = now or datetime.now(timezone.utc)
    c = [t for t in trips if not t["open"]]
    s = summarize(trips)
    days = len({datetime.fromtimestamp(t["entry_ts"] / 1000, CT).date() for t in c})
    L = ["# Trade memory", "",
         f"Generated {now.astimezone(CT):%Y-%m-%d %H:%M} CT from the fill ledger: {s['n']} closed round trips over {days} trading days"
         + (f", {len(trips) - len(c)} still open" if len(trips) > len(c) else "") + ".",
         "Facts computed from fills, not advice. Every figure carries its sample size; no rate is shown under "
         f"{MIN_N} closed trips. Hand-written reflections are in notes.md (read it too).", ""]
    if not c:
        return "\n".join(L + ["No closed round trips recorded yet.", ""])
    L += ["## Overall", f"- Net {_money(s['net'])} after ${s['fees']:.2f} fees, n={s['n']} ({s['wins']}W / {s['losses']}L / {s['scratch']} scratch)",
          f"- Average win {_money(s['avg_win'])}, average loss {_money(s['avg_loss'])}"
          + (f", payoff {s['payoff']}, win rate {s['win_rate']}%" if s["win_rate"] is not None else f" (win rate and payoff withheld: n<{MIN_N})"),
          f"- Held: winners {_dur(s['hold_win_s'])} on average, losers {_dur(s['hold_loss_s'])} on average (n={s['wins']} and {s['losses']})", ""]
    L += ["## Habits this ledger can see", "Each line is a count and the dollars it carried. A count under "
          f"{SMALL} is listed, not described.",
          f"- Re-entered within {REVENGE_S // 60} min of a losing exit on the same contract: n={s['revenge']['n']}, net {_money(s['revenge']['net'])}",
          f"- Added to a position (scale-in): n={s['scaled_in']['n']}, net {_money(s['scaled_in']['net'])}",
          f"- Entered against the desk's BIAS direction at that moment: n={s['counter_bias']['n']}, net {_money(s['counter_bias']['net'])}",
          f"- Entered after 14:55 CT: n={s['after_1455']['n']}, net {_money(s['after_1455']['net'])}",
          f"- Entered in the first 15 minutes of the 08:30 CT open: n={s['first_15_min']['n']}, net {_money(s['first_15_min']['net'])}", ""]
    L += [f"## Last {min(last, len(c))} closed trips (newest first)", "| CT | Contract | Side | Qty | Entry | Exit | Held | Net | BIAS at entry |", "|---|---|---|---|---|---|---|---|---|"]
    for t in sorted(c, key=lambda x: x["exit_ts"], reverse=True)[:last]:
        d = datetime.fromtimestamp(t["entry_ts"] / 1000, CT)
        x = t["ctx"]
        bias = "none" if not x else f"{x['grade']} {x['direction']}" + ("" if x["aligned"] is None else (" with" if x["aligned"] else " AGAINST"))
        L.append(f"| {d:%m-%d %H:%M} | {t['contract'].split('.')[3]} | {t['side']} | {t['qty']} | {t['avg_entry']} | {t['avg_exit']} | {_dur(t['hold_s'])} | {_money(t['net'])} | {bias} |")
    return "\n".join(L + [""])


def write_memory(root=None, ledger_path=None, bias_path=None, now=None):
    """Rebuild data/memory/trades.md from the ledger and bias log. Atomic (temp then rename); returns the trips."""
    root = Path(root or ROOT)
    ledger = Ledger(ledger_path or os.getenv("PX_JOURNAL_FILE") or root / "data" / "trades.jsonl")
    trips = attach_context(round_trips(ledger.rows), read_bias(bias_path or os.getenv("PX_BIAS_LOG_FILE") or root / "data" / "bias_log.jsonl"))
    out = Path(os.getenv("PX_MEMORY_DIR") or root / "data" / "memory")
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "trades.md.tmp"
    tmp.write_text(render_memory(trips, now), encoding="utf-8")
    os.replace(tmp, out / "trades.md")
    return trips
