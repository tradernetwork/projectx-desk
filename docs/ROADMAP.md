# Roadmap

What is done, what is next, in dependency order, and what is waiting on a decision. Update it in the same change that
moves an item. Rules that bind every item live in `AGENTS.md`; the reasons are in `docs/DECISIONS.md`.

## Follow-ups from the broker-MCP reading (2026-11-23)

- Journal execution facts are in (Round 39): entry slippage in ticks, size drift, longest loss streak. Slippage reads NA until
  something records an arrival quote.
- FOLLOW-UP, needs a `guard.py` change (do it as its own reviewed round, not from the journal): write the arrival quote (bid, ask, last
  and quote time at the commit point) and the displayed preview numbers (stop ticks, risk, size) with the `place` audit line, by adding
  allowlisted numeric keys to `AUDIT_KEYS`. Then a small journal reader can fill `data/arrivals.jsonl` or read the audit directly, and
  slippage stops being NA. Numbers only, never free text or the rendered sentence.

## Done (2026-10-30)

- A gateway logout no longer leaves a blind cache: unseed, REST reseed every `PX_RESEED_S`, one forced reconnect per 300 s, digest
  prints `UNKNOWN` instead of `NONE` when unverified (Round 23).
- A trailing stop (order type 5) counts as protection under the same conditions as a plain stop; `move_stop` refuses it; every
  cancel sweep cancels it (Round 24). `Guard.arm_trail` can place one (default OFF, Round 25); first sim probe still to do.

- Raw tape log (`tapelog.py`): every print as `ms,price,size,side,contract` in `data/tape/<ROOT>_<trading day>.tape`, default OFF
  (`PX_TAPE_LOG=1` plus `PX_TAPE_ROOTS`, nothing logged until roots are named), per-file and total caps, age prune, errors swallowed
  and counted, allowed in `PX_RECORD_ONLY`. `scripts/tape_size.py` reports bytes per day per root: enable one root first, measure,
  then widen. (Item numbers below are left as they were so references stay valid.)
- Trade-event detector (`tradewatch.py`, default OFF, `PX_TRADEWATCH=1`): no working stop N seconds after a position is seen
  (`PX_TRADEWATCH_NOSTOP_S`, 15), re-entry within 10 minutes of a losing exit, scale-in, entry after 14:55 CT, entry against the BIAS
  read. Habit definitions are `journal.py`'s. Fixed templates, a banner and chime (`watch.js` `freshEvents`) and `TRADEWATCH:` digest
  lines. Read-only, no path to the guard. Not yet seen against a live session; the coach (item 7 below) can speak first on these events.
  Round 39 adds STOP_WIDENED (a fixed stop moved away from price in the broker UI), CORRELATED (open positions on two roots of the
  equity-index group) and a `LOSS_ROOM:` digest line at 80 percent of the daily room used. Same status: unseen against a live session.

- Thesis card and quick buttons, first slice (item 6): `thesis.py` (allowlisted rows, append-only `data/thesis.jsonl`, pure fact checks:
  invalidation broken, scalp past its limit, stop wider than the invalidation), `GET/POST /api/thesis` (full desk only, same POST gate),
  a "thesis against what happened" section in `data/memory/trades.md`, and `static/thesis.js` (preset buttons, respected-level chips).
  Left for later: the invalidation alert through the phone notifier, a banner for the facts, voice fill.

- Read-only MCP tool `get_trade_memory` for the trade memory (roadmap item 9 prerequisite): dollars withheld unless
  `PX_MCP_MEMORY_DOLLARS=1`, `notes.md` only behind `PX_MCP_MEMORY_NOTES=1`, size-capped.
- Trendline declutter (display only): other-timeframe lines lose the timeframe text in their tag (dashed, 1m dotted, says it);
  `PXDeclutter.pickTrendLines` drops lines over 3 ATR from price and near-parallel copies of a stronger line, ranks by touches
  and recency, and caps the other-timeframe extras so at most 4 lines draw beside the chart's own. Per-timeframe toggles unchanged.

- Agent-set display toggles (Round 38): MCP tool `set_view(key, value)` over seven allowlisted chart flags, prefs in `data/view_prefs.json`,
  read-only `view` in `/state.json`, applied by `static/viewprefs.js`; `PX_AGENT_VIEW=1` lets the desk agent call it. Next: retire the
  toolbar buttons the trader no longer touches.

## Next, in order

**Gate: the sim probe.** Items 1, 2 (its probe) and the throughput test in 4 are settled only by a run of
[SIM_PROBE.md](SIM_PROBE.md) and its `scripts/probe_report.py` table (steps S1, S9 to S12, S14), not by reading the code. Nothing
below is ticked until that table shows PASS for the steps the item names; an UNKNOWN row is not a pass.

1. **Restart the desk and clear the latched `gateway_logout` halt.** Human step, flat only. Check the digest shows a seeded
   `POS:` and `ORD:` before trusting it, then watch one session.
   - Gate: SIM_PROBE.md step S1 PASS (audit `halt code=cleared` after the last `gateway_logout`, none after it).
2. **Trailing stops as the default entry stop.** A trade run with a hand-placed trailing stop is the case for it: the trail
   distance equals the initial stop distance, so 1R sizing is unchanged and the stop only ever tightens.
   - First a one-lot sim probe while flat: how the broker reads `trailPrice` (`docs/RESEARCH.md` says "absolute price"), whether
     `Order/modify` can change a live trail, how it behaves on a gap. Spec before any change to `guard.py`.
   - Gate: SIM_PROBE.md steps S9 to S12 PASS. The modify and gap questions are step S13, which the probe cannot settle from the
     desk (it never modifies a trail, and flat-by forbids holding a gap): they stay open after the probe.
   - Then a default-OFF switch (`PX_TRAIL_STOP`). Entry stops go in as type 5. Everything else keeps working as in Round 24.
4. **Tick charts.** Set `PX_TICK_BARS` to include 500 and 1000 (minimum 50, at most 4 sizes). Throughput test at the Sunday open:
   prints per second, hub-to-browser lag, CPU (SIM_PROBE.md step S14, recorded by hand; the desk has no hub-to-browser timer). Then a larger main-size tick chart with a 5m overlay (`trend.js` already maps one
   timeframe's lines onto another chart through time).
6. **Thesis card, second slice.** The first slice is in Done. Still to do: a phone alert and a chime when the invalidation breaks
   (a fixed template in `notify.py`, read-only), a banner for the facts, and voice filling the same fields.
7. **Coach service.** A chat dock in the desk that answers quickly, speaks first on a detected trade, and sees the page the trader
   sees. A fork of disclaw (MIT) with the Discord layer replaced by a desk adapter: `POST /ask` as SSE, `GET /events`, `POST /event`,
   sessions pinned by name. Claude subscription login only, no API keys, runs on the trader's own box.
   - Spike first: partial-message streaming and a reused CLI process, time to first words. Cold `claude -p` measured about 15 s
     even for a 60-word answer, so a plain port would not be faster.
   - Edit tier second: UI files only (no build step), on a branch, tests run, diff shown, trader approves. Python edits queue until
     flat. The order path (`guard.py`, order routes) is denied by path and by the guard-pin test, not by the prompt.
8. **Callout cross-check.** The coach says whether the newest analyst callout agrees with the open position. Only when the callout
   carries a symbol and side. Allowlisted fields only (`feed.py`), never callout text, opinion not signal, no path to the guard.
9. **Scheduled debriefs on the home agent.** A locked-down profile with only the desk MCP, a debrief after the close and a pre-open
   note, reading the trade memory. The read-only MCP tool for `data/memory/trades.md` now exists (`get_trade_memory`).

## Waiting on a decision

- Deep model for the coach: Opus or Sonnet (Opus burns more of the subscription).
- Edits from chat: a branch on the home box that the trader reviews here, or a patch applied from this checkout.
- Where scheduled output is delivered. A chat platform conflicts with the LAN-only decision, so nothing is wired until it is decided.
- Whether dollars belong in the trade-memory MCP tool (the AI surface otherwise carries no balances). Default is OFF (withheld); `PX_MCP_MEMORY_DOLLARS=1` turns them on.
- Prettier desk UI: three directions live in `design/prettier-desk/` (A conservative re-skin, B strong-fit re-composition, C divergent editorial), with screenshots and a slop audit in its README. Pick one to port into `projectx_desk/static/`; B is the recommendation.

## Known gaps

- `_retire_stop_after_close` cancels only a ticket's own stop by id; an external stop or trail left after a manual close is not swept.
- `state.is_protective_stop` treats a missing `status` as working because callers pass the broker's open-order list.
- No dedicated digest test for a trailing stop in the `ORD:` line.
- Orders placed in the broker's own UI bypass the desk's rails (`contract_flat`, one protection owner). The desk cannot refuse
  what it never sees.
