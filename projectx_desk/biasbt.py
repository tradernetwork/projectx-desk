"""Out-of-sample scoring of the BIAS rule from its own log (data/bias_log.jsonl). Pure, no I/O except `load`. Offline tool, never read by the desk.

The log holds, per tracked contract and closed 5m bar, the lean (LONG/SHORT), the letter, the ATR and the bar's close. That is enough to ask
the only question that matters: after a B or better, did price go the way the lean said, by more than a coin flip would?

Method (ideas borrowed from the usual quant-lab toolkit, reimplemented here):
  outcome      from the bar's close, a stop of `stop_k` ATR and a target of `rr` x that risk, checked on later 5m CLOSES for `horizon` bars
               (close-only: no intrabar path, no slippage or fees, so it flatters the rule if anything); time exit marks to market
  baseline     the same rows with the direction chosen by coin flip per block of `horizon` consecutive bars (neighbouring reads share price
               path, so flips are made per block, not per row). The p-value is how often the coin beats the lean
  walk-forward chronological folds, no refit (the rule is hand-set, not fitted): is the edge there in every window or in one lucky stretch?
  monte carlo  block bootstrap of the observed R sequence into trading paths: streak risk and how much of the loss room survives at `risk_pct`
  audit        mixed rule versions, duplicate bars that disagree (a repaint), gaps, and a structural no-lookahead guarantee (see `outcome`)
Overlapping windows mean the effective sample is about n / horizon, not n. A verdict needs enough of that; otherwise it says INSUFFICIENT."""
import json
import math
import random

BAR_MS = 300_000
LEANS = ("LONG", "SHORT")
GOOD = ("A", "B")
MIN_EFFECTIVE_N = 30          # independent-ish trades before any verdict
ALPHA = 0.05


def load(path):
    """Rows of the bias log, skipping blank and unparseable lines. Order is file order."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def series(rows, rule=None):
    """{contract: [row, ...]} sorted by bar_ts with one row per bar. Rows of another rule version (or with no version) are dropped when `rule`
    is given: scores are only comparable within one rule. The first row for a bar wins; `audit` reports the ones that disagreed."""
    by = {}
    for r in rows:
        if rule is not None and r.get("rule") != rule:
            continue
        if _num(r.get("bar_ts")) is None or _num(r.get("price")) is None or not isinstance(r.get("contract"), str):
            continue
        by.setdefault(r["contract"], {}).setdefault(int(r["bar_ts"]), r)
    return {c: [d[k] for k in sorted(d)] for c, d in by.items()}


def audit(rows, rule=None):
    """What would make a score untrustworthy. `repaints` = the same closed bar logged twice with a different close or lean."""
    seen, repaints, versions, no_atr = {}, 0, {}, 0
    for r in rows:
        versions[r.get("rule")] = versions.get(r.get("rule"), 0) + 1
        if rule is not None and r.get("rule") != rule:
            continue                                      # an older rule's rows are dropped from the score, so their repaints do not count
        k = (r.get("contract"), r.get("bar_ts"))
        if k in seen:
            a = seen[k]
            if a.get("price") != r.get("price") or a.get("direction") != r.get("direction") or a.get("grade") != r.get("grade"):
                repaints += 1
        else:
            seen[k] = r
    ser = series(rows, rule)
    gaps = 0
    for rs in ser.values():
        gaps += sum(1 for a, b in zip(rs, rs[1:]) if b["bar_ts"] - a["bar_ts"] != BAR_MS)
        no_atr += sum(1 for r in rs if not (_num(r.get("atr")) and r["atr"] > 0))
    return {"rows": len(rows), "scored_rows": sum(len(v) for v in ser.values()), "contracts": len(ser), "rule_versions": versions,
            "mixed_rules": len(versions) > 1,
            "repaints": repaints, "gaps": gaps, "no_atr": no_atr}


def outcome(rs, i, sgn, horizon=6, stop_k=1.5, rr=1.5):
    """R multiple of entering `sgn` (+1 long, -1 short) at the close of rs[i]. None when it cannot be scored honestly: a missing bar inside
    the window, no ATR, or the window runs past the data.

    NO LOOKAHEAD, by construction: the entry and the risk use only rs[i]; rs[i+1 ..] is read only as FUTURE prices to be judged against it,
    and nothing at or before i-1 is read at all. A test mutates the past and the far future to pin that."""
    if i + horizon >= len(rs):
        return None
    a = _num(rs[i].get("atr"))
    p0 = _num(rs[i].get("price"))
    if not a or a <= 0 or p0 is None:
        return None
    risk = stop_k * a
    t0 = rs[i]["bar_ts"]
    for j in range(1, horizon + 1):
        pj = _num(rs[i + j].get("price"))
        if pj is None or rs[i + j]["bar_ts"] != t0 + j * BAR_MS:
            return None
        move = sgn * (pj - p0)
        if move <= -risk:
            return -1.0
        if move >= rr * risk:
            return float(rr)
    return max(-1.0, min(float(rr), move / risk))      # time exit, marked to market


def trades(ser, horizon=6, stop_k=1.5, rr=1.5):
    """One record per scoreable lean: its grade, the R it earned, the R the opposite side would have, and its time, contract and block id."""
    out = []
    for c, rs in ser.items():
        for i, r in enumerate(rs):
            d = r.get("direction")
            if d not in LEANS:
                continue
            sgn = 1 if d == "LONG" else -1
            r_for, r_against = outcome(rs, i, sgn, horizon, stop_k, rr), outcome(rs, i, -sgn, horizon, stop_k, rr)
            if r_for is None or r_against is None:
                continue
            out.append({"contract": c, "t": r["bar_ts"], "grade": r.get("grade"), "r": r_for, "r_opp": r_against, "block": (c, i // horizon)})
    out.sort(key=lambda x: x["t"])
    return out


def summary(rs_):
    n = len(rs_)
    if not n:
        return {"n": 0, "win": None, "mean_r": None}
    return {"n": n, "win": round(sum(1 for x in rs_ if x > 0) / n, 3), "mean_r": round(sum(rs_) / n, 3)}


def by_grade(tr):
    out = {g: summary([t["r"] for t in tr if t["grade"] == g]) for g in ("A", "B", "C", "D")}
    out["A+B"] = summary([t["r"] for t in tr if t["grade"] in GOOD])
    out["all leans"] = summary([t["r"] for t in tr])
    return out


def coin_flip_p(tr, grades=GOOD, iters=5000, seed=1):
    """P(a coin picking the direction per block does at least as well as the lean) on the trades with these grades. Small = the lean beats luck."""
    sel = [t for t in tr if t["grade"] in grades]
    if not sel:
        return None
    obs = sum(t["r"] for t in sel) / len(sel)
    blocks = {}
    for t in sel:
        blocks.setdefault(t["block"], []).append(t)
    groups = list(blocks.values())
    rng = random.Random(seed)
    ge = 0
    for _ in range(iters):
        tot = 0.0
        for g in groups:
            flip = rng.random() < 0.5
            tot += sum(t["r_opp"] if flip else t["r"] for t in g)
        if tot / len(sel) >= obs - 1e-12:
            ge += 1
    return round((ge + 1) / (iters + 1), 4)


def folds(tr, grades=GOOD, k=3):
    """Chronological folds of the chosen trades, equal counts, no refit."""
    sel = [t for t in tr if t["grade"] in grades]
    if len(sel) < k:
        return []
    size = len(sel) / k
    return [summary([t["r"] for t in sel[int(round(i * size)):int(round((i + 1) * size))]]) for i in range(k)]


def monte_carlo(tr, grades=GOOD, risk_pct=0.10, paths=5000, length=40, block=6, seed=1):
    """Block bootstrap of the observed R sequence into `paths` runs of `length` trades. Room compounds: room *= 1 + risk_pct * R, so a
    loss costs risk_pct of what is LEFT of the loss room, as the desk sizes it. Returns streak and survival numbers."""
    seq = [t["r"] for t in tr if t["grade"] in grades]
    if len(seq) < block * 2:
        return None
    rng = random.Random(seed)
    streak3 = worst = 0
    mins, longest = [], []
    for _ in range(paths):
        path = []
        while len(path) < length:
            s = rng.randrange(0, len(seq) - block + 1)
            path.extend(seq[s:s + block])
        path = path[:length]
        room, lo, run, best = 1.0, 1.0, 0, 0
        for x in path:
            room *= 1.0 + risk_pct * x
            lo = min(lo, room)
            run = run + 1 if x < 0 else 0
            best = max(best, run)
        mins.append(lo)
        longest.append(best)
        streak3 += best >= 3
        worst += best >= 10
    mins.sort()
    longest.sort()
    q = lambda a, f: a[min(len(a) - 1, int(f * len(a)))]
    return {"paths": paths, "length": length, "risk_pct": risk_pct, "p_3_losses_in_a_row": round(streak3 / paths, 3),
            "p_10_losses_in_a_row": round(worst / paths, 4), "median_longest_losing_streak": q(longest, 0.5),
            "room_left_p05": round(q(mins, 0.05), 3), "room_left_median_low": round(q(mins, 0.5), 3)}


def verdict(tr, p, fold_stats, grades=GOOD, horizon=6):
    sel = [t for t in tr if t["grade"] in grades]
    eff = len(sel) / horizon
    if eff < MIN_EFFECTIVE_N:
        return f"INSUFFICIENT: {len(sel)} trades is about {eff:.0f} independent ones (need {MIN_EFFECTIVE_N}); keep logging, this says nothing yet"
    mean = sum(t["r"] for t in sel) / len(sel)
    pos = sum(1 for f in fold_stats if f["mean_r"] is not None and f["mean_r"] > 0)
    if mean > 0 and p is not None and p < ALPHA and fold_stats and pos >= len(fold_stats) - 1:
        return f"EDGE SHOWN: mean {mean:+.2f}R, coin flip p={p}, positive in {pos}/{len(fold_stats)} folds (still close-only, no costs)"
    return f"NO EDGE SHOWN: mean {mean:+.2f}R, coin flip p={p}, positive in {pos}/{len(fold_stats)} folds"


def report(rows, rule="v2.1", horizon=6, stop_k=1.5, rr=1.5, risk_pct=0.10, k=3, seed=1, iters=5000):
    ser = series(rows, rule)
    tr = trades(ser, horizon, stop_k, rr)
    p = coin_flip_p(tr, GOOD, iters, seed)
    fs = folds(tr, GOOD, k)
    return {"params": {"rule": rule, "horizon_bars": horizon, "stop_atr": stop_k, "target_r": rr, "risk_pct": risk_pct, "folds": k, "seed": seed},
            "audit": audit(rows, rule), "by_grade": by_grade(tr), "coin_flip_p_A_B": p, "folds_A_B": fs,
            "monte_carlo_A_B": monte_carlo(tr, GOOD, risk_pct, seed=seed), "verdict": verdict(tr, p, fs, GOOD, horizon)}
