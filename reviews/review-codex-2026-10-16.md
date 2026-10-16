# Codex second opinion — 2026-10-16

Reviewed commit `db1be93` with a clean initial working tree. This is a review, not an implementation change. No broker credentials were used for trading, no real orders were submitted, and application code was left unchanged.

The central guard, single-use tickets, read/write separation, explicit ambiguity handling, and breadth of offline tests are valuable. The highest-priority remaining problems are lifecycle races: a known position can lose protection, a late stop can become a new position, and conditions checked before awaited reads can change before an entry is sent. I would resolve the P1 findings before the real sim rehearsal.

## Scope and verification

- Read `AGENTS.md`, `docs/DECISIONS.md`, `docs/LIMITATIONS.md`, existing review context, and the relevant guard, state, HTTP, digest, tape, and UI code.
- Started a separate mock desk at <http://127.0.0.1:8623/?ai=1> with mock gateway port 8710, temporary guard persistence, a separate `/tmp` flow file, and external feed/GEX credentials disabled in the launch environment. Verified its digest reports mock SIM, both hubs UP, seeded state, quotes, and sizing.
- Browser inspection was **blocked**: browser inventory returned no browsers and explicitly opening Chrome returned `Browser is not available: chrome`. UI observations below come from source inspection, not a rendered visual assessment. No screenshots were saved.
- Ran deterministic reproductions using the existing scripted-broker fixtures. Results and runnable reproduction are included below. These exercise actual guard methods; only broker behavior and timing are scripted.
- Full suite: `.venv/bin/python -m pytest -q` — **529 passed, 1 warning in 22.90s**, run outside the socket-restricted sandbox. The warning is the FastAPI/Starlette TestClient deprecation for httpx. The interrupted initial run and socket-restricted run are not counted as successful verification.

Severity: **P1** = protection/order correctness; **P2** = privacy, misleading data, or ticket lifecycle correctness. Reproduced behavior is distinguished from possible downstream broker consequences.

## 1. P1 — Missing stop causes the desk to discard ownership of its still-open position

**Location:** `px_guard.py:891–910`, `protect_open_positions()`.

An owned ticket with an ACKed stop is retired when that order ID disappears from the broker's open orders. `_close_intents(owner)` also marks the entry intent CLOSED. The following ownership check therefore finds no active intent and labels the position `EXTERNAL_UNPROTECTED`, without flattening or halting.

**Reproduction:** place an entry, fill it, ACK its stop, leave that exact position open, then have the broker return no working stops during recovery. No hand entry or intervening flat position is required. Observed: `EXTERNAL_UNPROTECTED`, position still open, zero additional writes, halt false.

**Impact:** cancellation or disappearance of a stop can leave the desk's original position naked. Retiring its ticket also removes that ticket's open risk from `_open_risk()`.

**Suggested change:** retain ownership until a close/replacement has been proved. A missing stop with unresolved position identity should trigger an explicit protection failure and halt; use the agreed recovery policy for provably owned exposure. Do not infer a hand entry solely from a missing stop. Preserve the rule that genuinely external positions are never automatically touched.

**Test gap:** `tests/test_px_lifecycle.py:390` expects the external classification after a stop-fill/hand-entry story, but supplies no broker evidence distinguishing that story from simple stop cancellation. Add the unchanged-position case and make the ownership decision depend on evidence.

## 2. P1 — A stop ACK after an ordinary position close is left working

**Locations:** `px_guard.py:843–847`, `943–951`, `966–971`.

A zero-size event marks a filled PLACED ticket CLOSED and cancels its watchdog, but does not set `abandoned`. If its stop request is still in flight, the subsequent ACK takes the normal ACKED branch. The post-ACK size check only handles a nonzero current position, so a zero position causes no cancellation.

**Reproduction:** pause the stop reply after a fill; close the position while that request is pending; deliver the zero-size event; then release the stop ACK. Observed: ticket CLOSED, stop ACKED, `abandoned=False`, no cancellation, no halt.

**Impact:** a surviving standalone sell stop after a long closes can later open an unintended short. The watchdog late-ACK handling does not cover this ordinary-close path.

**Suggested change:** closing a ticket must invalidate pending protection and retire confirmed standalone protection as appropriate. Reuse the existing late-stop cancellation/orphan handling when an ACK arrives for a closed position. Prove the broker book is clear; never assume stop ACK implies useful protection.

## 3. P1 — Final commit check omits hub health and current market drift

**Location:** `px_guard.py:701–706`, following `_rest_matches_live()`.

`_evaluate()` and the drift check run before the final awaited position/order reads. The synchronous commit check then rechecks halt, expiry, and quote age only. A quote can be very fresh but materially different; the user hub can also have dropped during those reads.

**Reproduction A:** set `user_hub.up=False` inside the final position read. Observed: `place_ok=True` and one entry write.

**Reproduction B:** change the fresh ask from synthetic `5000.25` to `5010.25` during that read. This is 40 ticks, exceeding the configured 8-tick drift limit. Observed: `place_ok=True` and one entry write using the original ticket.

**Impact:** the entry can be sent while the fill/protection event channel is unavailable, or at a market reference outside the drift promise and staged risk calculation.

**Suggested change:** after all awaited reads, synchronously validate every mutable condition needed to authorize the commit: hub/seed health, current quote price and drift, current sizing/headroom, session/deadline, and current ticket eligibility. If a stable evaluation snapshot cannot be established, refuse and require restaging. Keep the final checks free of further asynchronous gaps.

## 4. P2 — Restaging during placement does not prevent the superseded ticket from being sent

**Locations:** `px_guard.py:584–587`, `place()` final commit block at `701–716`.

`place()` holds `_lock`, but `preview()` does not acquire it. A preview can supersede the ticket while placement awaits REST. The commit block does not recheck `t.state == STAGED` and overwrites SUPERSEDED with PLACING.

**Reproduction:** stage a second ticket from inside the first ticket's final position read. Observed: the first ticket is still successfully placed.

**Impact:** the order sent can be a ticket the desk has already replaced, while the replacement remains staged. This violates the restaging/single-current-intent model even though repeat placement of the same ticket is locked.

**Suggested change:** serialize the relevant staging transition with commit, or use a ticket generation/current-intent check at commit and refuse a superseded ticket. Do not hold a global lock in a way that delays flatten or stop protection.

## 5. P1 — Flatten can report success without verifying positions after cancelling orders

**Location:** `px_guard.py:1053`, `1062–1076`, `1087`.

Flatten snapshots positions before cancelling working orders. When that first snapshot is empty, it sets `flat=True` without any subsequent position read. A resting entry can fill between the first position read and the order/cancellation reads, then disappear from the working-order book.

**Reproduction:** return an empty position list on the first read, with subsequent position reads configured to return a position; return an empty working-order list after the simulated fill. Observed: `flat=True`, just one position read, halt false. The guard never asks for the new position.

**Impact:** FLATTEN can claim success while an entry that raced cancellation remains open. Even a failed initial REST read falling back to an empty desk view can reach the same unverified success branch.

**Suggested change:** always perform a final broker position verification after order cancellation, including an initially empty position snapshot. If exposure appeared, close/verify under the existing policy; if unreadable, return NOT_FLAT and halt. Retain no-replay handling for ambiguous writes.

## 6. P2 — Stream mode leaks the new dollar projection fields into the API, digest, and UI

**Locations:** `px_digest.py:610–615`, `683–684`; `static/desk.js:217–223`.

`redact_stream()` removes the older dollar fields but leaves `size.drawdown_left`, `size.daily_left`, and `size.heat_usd`. The digest prints these in `SIZE_IF_STOPPED`, and `renderSize()` prints them in the always-created plan text outside the sensitive slots.

**Verification:** fetched `/ai/digest?contract=CON.F.US.MES.Z26&risk_pct=0.05&stream=1` from the mock. It reports `STREAM=ON` and still contains numeric `drawdown_room_left`, `daily_room_left`, and `heat_after_usd`. Values are intentionally omitted here; no real account data was used or stored.

**Impact:** the new plan bypasses the intended dollar redaction, including visible text when screen sharing. This is distinct from the already-documented unredacted preview response.

**Suggested change:** redact these new fields server-side and render a percentage-only/hidden alternative in stream mode. Also pass stream mode to the browser's `/state.json` fetch (`static/desk.js:158` currently omits it). Add coverage for projection fields and rendered plan/digest transitions, not just the older dollar keys.

## 7. P2 — An ongoing tape outage ages into a falsely complete zero-volume window

**Locations:** `px_flow.py:116–131`, `137–154`; `static/desk.js:984–989`.

`mark_gap()` sets an internal flag and adds a partial minute, but snapshot exports only partial markers attached to actual volume buckets. A hole is not added until the next valid trade arrives. `summary()` does not consider an unresolved gap, so once the last partial bucket falls outside the window, absent buckets are treated as complete zero volume.

**Reproduction:** record 65 consecutive minutes, mark the feed down, then summarize 10 minutes later without another trade. Observed for the last 5 minutes: buy 0, sell 0, delta 0, `complete=True`.

**Impact:** disconnection or tape eviction can look like a quiet, fully observed market. The chart also lacks the open-ended hole needed to blank that interval while the outage is ongoing. On return, the newly recorded closed hole repairs history, but not the misleading view during the outage.

**Suggested change:** expose unresolved coverage loss as an open interval through now (or an explicit active-gap state). Both the chart and summary should mark every overlapping window incomplete until receiving resumes. Retain the distinction between a healthy quiet tape and an unavailable one.

## Additional review observations

- **Mock startup is not fully configuration-isolated.** `run_dev.sh:20–24` overrides the broker connection and guard persistence, but `px_app.main()` still loads remaining `.env.projectx` keys. My first attempt loaded local feed settings, flat-by, MLL floor, bind, and sizing settings by name. I explicitly disabled external advisory integrations for the successful review launch. Consider a dedicated development environment or explicit loader opt-out, and isolate `PX_FLOW_FILE` by default so mock volume cannot contaminate normal flow persistence. I did not inspect or reproduce the user's private values in this report.
- **Documentation has drift.** `docs/LIMITATIONS.md` still says GEX is always unavailable despite the implemented provider, and describes behavioral losses as separate closing fills although `closed_trades()` now groups by order ID. Its statement that none of the real gateway behavior is verified also needs reconciliation with its later note documenting a real subscription/bar check. Keep unresolved limitations, but distinguish verified read behavior from unverified trading behavior.
- **UX assessment remains incomplete.** The source has useful accepted-vs-filled wording and strong readback/protection language. I cannot assess layout, chart legibility, overflow, mobile behavior, focus, or stream-mode DOM transitions without a connected browser. A visual pass is still required; this report does not claim one happened.
- **External-position handling is deliberate.** I did not treat the documented refusal to manipulate genuinely external positions as a bug. Finding 1 concerns losing evidence of the desk's own position.

## Suggested order of work

1. Fix and regression-test ownership/protection loss and late stop ACKs (1–2).
2. Make entry commit validate current authorization and ticket state (3–4).
3. Verify the final broker book on every flatten path (5).
4. Repair stream redaction and ongoing flow-gap reporting (6–7).
5. Isolate development configuration, refresh limitations, then perform a browser pass and the documented operator-led real sim rehearsal.

## Deterministic reproduction evidence

The temporary script `/tmp/projectx_review_repro.py` uses existing test fixtures and temporary directories, and was run with `.venv/bin/python /tmp/projectx_review_repro.py`. Its output was:

```text
missing_stop {'CON.F.US.MES.Z26': 'EXTERNAL_UNPROTECTED'} halted= False additional_writes= 0 position_open= True
hub_down place_ok= True writes= 1 hub_up= False ask= 5000.25
drift place_ok= True writes= 1 hub_up= True ask= 5010.25
supersede place_ok= True writes= 1 hub_up= True ask= 5000.25
stop_ack_after_natural_close ticket= CLOSED stop= ACKED abandoned= False writes= 1 halted= False
flatten_fill_race flat= True position_reads= 1 halted= False
ongoing_gap_5m {'buy': 0, 'sell': 0, 'unknown': 0, 'delta': 0, 'complete': True}
```

All prices and identifiers in this reproduction are repository fixture data. These observations do not establish any real gateway execution behavior.

### Reproduction script

Save this as `/tmp/projectx_review_repro.py` and run it from this checkout using `.venv/bin/python /tmp/projectx_review_repro.py`. Adjust the two checkout paths if needed. It writes only to a temporary directory.

```python
import asyncio
import sys
import tempfile
from pathlib import Path
sys.path[:0] = ['/home/mpha/projects/projectx-desk', '/home/mpha/projects/projectx-desk/tests']
from test_px_guard_protect import build2, fill
from test_px_guard_place import staged, go, CID
from px_flow import FlowStore, summary, MIN_MS

async def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        g, live, clock, gw = build2(root / 'missing')
        t = await staged(g)
        await go(g, t)
        p = fill(live)
        gw.pos = [p]
        await g.handle_position(p)
        before = len(gw.writes)
        result = await g.protect_open_positions()
        print('missing_stop', result, 'halted=', g.halted(), 'additional_writes=', len(gw.writes)-before, 'position_open=', bool(gw.pos))

        for case in ('hub_down', 'drift', 'supersede'):
            g, live, clock, gw = build2(root / case)
            t = await staged(g)
            orig = gw.open_positions
            async def during_read(aid):
                if case == 'hub_down':
                    g.user_hub.up = False
                elif case == 'drift':
                    live.quotes[CID].update(bestAsk=5010.25, bestBid=5010.0, lastPrice=5010.0, _rx=clock())
                else:
                    await staged(g)
                return await orig(aid)
            gw.open_positions = during_read
            r = await go(g, t)
            print(case, 'place_ok=', r.ok, 'writes=', len(gw.writes), 'hub_up=', g.user_hub.up, 'ask=', live.quotes[CID]['bestAsk'])

        g, live, clock, gw = build2(root / 'late_stop')
        t = await staged(g)
        await go(g, t)
        release = asyncio.Event()
        async def delayed_stop(path, body):
            gw.writes.append((path, dict(body)))
            await release.wait()
            return {'success': True, 'orderId': 900}
        gw.write = delayed_stop
        async def long_sleep(_):
            await asyncio.sleep(3600)
        g.sleep = long_sleep
        task = asyncio.create_task(g.handle_position(fill(live)))
        await asyncio.sleep(0)
        live.positions.clear()
        await g.handle_position(dict(id=1, contractId=CID, type=1, size=0))
        release.set()
        await task
        print('stop_ack_after_natural_close', 'ticket=', t.state, 'stop=', t.stop_state, 'abandoned=', t.abandoned, 'writes=', len(gw.writes)-1, 'halted=', g.halted())

        g, live, clock, gw = build2(root / 'flatten_race')
        count = 0
        async def positions(aid):
            nonlocal count
            count += 1
            return [] if count == 1 else [dict(id=1, contractId=CID, type=1, size=1)]
        gw.open_positions = positions
        async def orders(aid):
            return []
        gw.open_orders = orders
        r = await g.flatten()
        print('flatten_fill_race', 'flat=', r.flat, 'position_reads=', count, 'halted=', g.halted())

    f = FlowStore()
    base = 1791982800000 // MIN_MS * MIN_MS
    for i in range(65):
        f.add(CID, base+i*MIN_MS, 1, 0)
    f.mark_gap(CID, base+65*MIN_MS)
    s = summary(f, CID, base+75*MIN_MS)
    print('ongoing_gap_5m', s['w'][5])

asyncio.run(main())
```
