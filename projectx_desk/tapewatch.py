"""Per-contract trade-print age (read-only).

2026-11-17: MGC printed nothing from 08:48 CT while the market hub reported up with frames flowing (the quotes and the other
contracts kept the socket busy), so no alarm fired and the MGC bias rows silently lost the flow vote (FLOW_NA). A hub that is
alive does not prove each contract's trade subscription is. This module measures the age of the last print per tracked contract,
flags a stale one while the market is open and the hub is up, and re-sends that one subscription at most once per cooldown.
It never touches the guard, the client or an order route: it only reads `LiveState` and re-subscribes a market-data stream.

PX_TAPE_STALE_S: seconds without a print before a tracked contract is flagged (default 300, floor 60, 0 = off, garbled = 300).
"""
import asyncio
import os
import time

from projectx_desk import tradeday

DEFAULT_STALE_S = 300.0
FLOOR_S = 60.0
RESUB_COOLDOWN_S = 300.0
POLL_S = 30.0


def stale_s():
    raw = (os.getenv("PX_TAPE_STALE_S") or "").strip()
    if not raw:
        return DEFAULT_STALE_S
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_STALE_S
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return DEFAULT_STALE_S
    return 0.0 if v == 0 else max(FLOOR_S, v)


def evaluate(tracked, last_print, tape_start, now, thresh, hub_up, is_open):
    """{root: {"age_s": seconds since the last print or None if none yet, "stale": bool}} for every tracked root with a contract.
    `now`, the print times and the start times share one monotonic clock. A contract that never printed ages from its
    subscription start. Stale needs the threshold on, the hub up and the market open: a closed market prints nothing."""
    out = {}
    for root, cid in (tracked or {}).items():
        if not cid:
            continue
        lp = last_print.get(cid)
        ref = lp if lp is not None else tape_start.get(cid)
        age = None if lp is None else round(max(0.0, now - lp), 1)
        stale = bool(thresh and hub_up and is_open and ref is not None and now - ref > thresh)
        out[root] = {"age_s": age, "stale": stale}
    return out


async def step(live, memo, now, tracked, hub_up, is_open, quiet, thresh):
    """One pass: re-send the trade subscription of each stale tracked contract, at most once per RESUB_COOLDOWN_S per contract.
    `memo` (cid -> time of the last resubscribe) belongs to the caller. Returns the roots it resubscribed."""
    if quiet or not thresh:
        return []
    done = []
    ev = evaluate(tracked, live._last_print, live._tape_start, now, thresh, hub_up, is_open)
    for root, e in ev.items():
        cid = tracked[root]
        if not e["stale"]:
            continue
        last = memo.get(cid)
        if last is not None and now - last < RESUB_COOLDOWN_S:
            continue
        memo[cid] = now
        try:
            await live.resubscribe_tape(cid)
        except Exception as ex:  # noqa: BLE001 - a failed resend is retried after the cooldown, never fatal
            print(f"tape: resubscribe failed {root} {type(ex).__name__}", flush=True)
            continue
        print(f"tape: stale print age, resubscribed {root}", flush=True)
        done.append(root)
    return done


def health(app, now=None):
    """The `feed.tape` block for state and digest: {root: {age_s, stale}} for the tracked roots, {} when nothing is tracked."""
    st = app.state
    live, market = getattr(st, "live", None), getattr(st, "market", None)
    if live is None or market is None:
        return {}
    return evaluate(getattr(st, "tracked", None), live._last_print, live._tape_start, time.monotonic() if now is None else now,
                    stale_s(), bool(market.up), tradeday.market_open())


async def loop(app, quiet):
    """Background pass every POLL_S. `quiet(app)` is the desk's weekend-quiet predicate."""
    memo = {}
    while True:
        await asyncio.sleep(POLL_S)
        try:
            st = app.state
            if getattr(st, "market", None) is None or getattr(st, "live", None) is None:
                continue
            await step(st.live, memo, time.monotonic(), getattr(st, "tracked", None) or {}, bool(st.market.up),
                       tradeday.market_open(), quiet(app), stale_s())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - an alarm must never break the desk
            print(f"tape: watch failed {type(e).__name__}", flush=True)
