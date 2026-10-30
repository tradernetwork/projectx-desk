# Roadmap

What is done, what is next, in dependency order, and what is waiting on a decision. Update it in the same change that
moves an item. Rules that bind every item live in `AGENTS.md`; the reasons are in `docs/DECISIONS.md`.

## Done (2026-10-30)

- A gateway logout no longer leaves a blind cache: unseed, REST reseed every `PX_RESEED_S`, one forced reconnect per 300 s, digest
  prints `UNKNOWN` instead of `NONE` when unverified (Round 23).
- A trailing stop (order type 5) counts as protection under the same conditions as a plain stop; `move_stop` refuses it; every
  cancel sweep cancels it (Round 24). The desk does not place one yet.

## Next, in order

1. **Restart the desk and clear the latched `gateway_logout` halt.** Human step, flat only. Check the digest shows a seeded
   `POS:` and `ORD:` before trusting it, then watch one session.
2. **Trailing stops as the default entry stop.** A trade run with a hand-placed trailing stop is the case for it: the trail
   distance equals the initial stop distance, so 1R sizing is unchanged and the stop only ever tightens.
   - First a one-lot sim probe while flat: how the broker reads `trailPrice` (`docs/RESEARCH.md` says "absolute price"), whether
     `Order/modify` can change a live trail, how it behaves on a gap. Spec before any change to `guard.py`.
   - Then a default-OFF switch (`PX_TRAIL_STOP`). Entry stops go in as type 5. Everything else keeps working as in Round 24.
3. **Raw tape log.** Append-only prints (time, price, size, aggressor side) so any tick size can be rebuilt after the fact; the gateway
   has no tick history. Measure the daily size per root before enabling it for all of them. Write with `print(..., file=f)`.
4. **Tick charts.** Set `PX_TICK_BARS` to include 500 and 1000 (minimum 50, at most 4 sizes). Throughput test at the Sunday open:
   prints per second, hub-to-browser lag, CPU. Then a larger main-size tick chart with a 5m overlay (`trend.js` already maps one
   timeframe's lines onto another chart through time).
5. **Trade-event detector in the desk.** Read-only background task like `notify.py`: no stop after N seconds, re-entry after a loss,
   scale-in, late entry, entry against the BIAS read. Fixed templates, a banner and chime on the desk. No model needed to detect.
6. **Thesis card and quick buttons.** The trader states the idea in a few taps before or right after an entry: type (reversal hold,
   scalp, fade the range, trend add), target, invalidation level, time limit. Presets prefill it, and respected levels from
   `respect.py` show as tappable chips for the invalidation and the target, so nothing is typed mid-trade. The desk then holds the
   trader to their own thesis with plain numbers: an alert when the invalidation level breaks, a timer when a scalp overruns its
   limit, a flag when the stop sits wider than the invalidation, and a line in the trade memory (thesis against what happened) for the
   debrief. It reports facts and never grades the idea. Voice later fills the same fields, so nothing here is thrown away. Display and
   logging only, no path to the guard.
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
   note, reading the trade memory. Needs a read-only MCP tool for `data/memory/trades.md`.

## Waiting on a decision

- Deep model for the coach: Opus or Sonnet (Opus burns more of the subscription).
- Edits from chat: a branch on the home box that the trader reviews here, or a patch applied from this checkout.
- Where scheduled output is delivered. A chat platform conflicts with the LAN-only decision, so nothing is wired until it is decided.
- Whether dollars belong in the trade-memory MCP tool (the AI surface otherwise carries no balances).

## Known gaps

- `_retire_stop_after_close` cancels only a ticket's own stop by id; an external stop or trail left after a manual close is not swept.
- `state.is_protective_stop` treats a missing `status` as working because callers pass the broker's open-order list.
- No dedicated digest test for a trailing stop in the `ORD:` line.
- Orders placed in the broker's own UI bypass the desk's rails (`contract_flat`, one protection owner). The desk cannot refuse
  what it never sees.
