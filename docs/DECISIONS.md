# Design decisions (rounds 1 and 2, 2026-10-14)

Six agents (guard designer, red team, trader UX; then trader's advocate, risk engineer,
implementer) argued the contested points. This is the reconciled ruling. Where they
disagreed, the reasoning for the pick is given. Items marked UNVERIFIED must be proven
against a sim account before they are relied on.

> **Read this first: intent vs built.** This file records what was decided and why. Some rulings describe
> behaviour that is **not built yet**. `AGENTS.md` and the code are the source of truth for what exists.
>
> | Ruling | Status |
> |---|---|
> | D1 600 ms hold, readback on screen 1.5 s | built (UI) |
> | D1 out-of-band confirm code (`PX_CONFIRM_MODE=code`) and 3 s minimum stage-to-place delay for live or qty 2+ | **not built** |
> | D3 UNKNOWN on an ambiguous entry write, persistent halt, flatten-and-halt on a missing stop | built |
> | D3 auto-clear of UNKNOWN after a flat, consistent re-read | **not built**; only a human clears a halt |
> | D4 stream mode removes account and dollar nodes from the DOM | built (UI), with the caveats in docs/LIMITATIONS.md |
> | D5 reconcile by `customTag` on boot and on UNKNOWN | built as a manual call (`POST /api/reconcile`); **not automatic** |
> | D5 `customTag` search actually returns the tag on a real gateway | **unverified** |
> | D7 prefetch REST state at STAGE | **not built**; the fresh-state re-read happens at place |
> | D9 MCP cannot place | built: no tool exists. `PX_MCP_PLACE` is a reserved flag that no route uses |
> | Loopback only | **changed**: loopback is the default; a non-loopback bind is allowed only with `PX_OPERATOR_TOKEN` and `PX_ALLOWED_HOSTS` (built, tested). The 2026-10 decision to allow container and LXC deployment is why. |
| Live-account refusal | **policy, plus** `PX_ALLOW_LIVE=1` and OCO-only market entries in code; there is no code block beyond those |

## Threat model, stated plainly

Codex in Chrome drives the page with real browser input. **Nothing in the browser can tell its
click from the operator's**: not a hold duration, not a canvas challenge, not `isTrusted`. So browser
friction is an anti-fat-finger measure, not a security control. The real controls are server-side
(caps, ticket binding, fresh-state gate, qty cap, sim-first) and, for live money, a confirm channel
the voice agent has no tool for.

## Rulings

| # | Decision | Ruling |
|---|---|---|
| D1 | Place control | **Friction scales with consequence.** Sim account, qty 1: 600 ms hold on a separate PLACE bar, readback on screen at least 1.5 s concurrent with the hold, no other delay (a blanket 3 s delay costs fills and would be bypassed). Live/eval-funded account, or qty 2+: out-of-band confirm (`PX_CONFIRM_MODE=code`): a 6-char single-use code bound to the ticket hash is pushed to a channel the browser agent cannot read (phone/ntfy to a secret topic, or a local TTY), typed by the operator; 120 s TTL, 3 wrong tries kills the ticket, minimum 3 s stage-to-place. Stated residual risk: if the agent also has shell access to that channel, it fails; then qty cap and sim-first are the controls. The hold is NOT claimed as security. Confirm provider is an interface; hold ships first, code gate next. |
| D2 | Brackets | `PX_BRACKET_MODE` must be set explicitly (`position` or `oco`). Default `unset` means **opens are refused** (never guess; a wrong guess still creates a rejected order). Sim: `position` mode allowed with stop submitted on fill confirm and a 5 s stop-ack watchdog (flatten + halt on miss); orphan stops are cancellable on sim. **Live: only native atomic bracket (`oco`), market entry only, no limit opens** (live stops cannot be cancelled via API, so an orphan stop later becomes a fresh position). Verified in the sim rehearsal (I8) before any live use. |
| D3 | UNKNOWN / missed stop | Any write without a definitive reply enters UNKNOWN, writes the halt file (fsync, survives restart), never replays. Auto-clear allowed only if REST re-read plus reconcile-by-tag shows a consistent FLAT state; if a position is open: flatten, verify flat by 1 s re-reads up to 10 s, capped opposite market order as fallback, then stay halted for a human. Missed stop ack at 5 s: flatten + halt. |
| D4 | Stream mode | Two tiers, DOM removal not CSS. Removed (and server-redacted in `/state.json` and the digest): account id/suffix, balance, equity, buying power, absolute P&L, tokens, confirm codes. Kept so voice workflow still works: symbol, side, qty, entry/stop prices, ticket readback, size box (risk as percent of budget). `?stream=1` or toggle; reveal button auto-re-hides in 30 s. Phase 3b. |
| D5 | customTag | Guard generates it, ignores caller tags: `px-<ticket_hash8>-<n>`. Intent record (ticket, tag, SENDING) is written to disk with fsync BEFORE the HTTP call; on boot and on UNKNOWN, reconcile by searching orders/positions by tag. Not found: never resend, mark FAILED_UNCONFIRMED for a human. UNVERIFIED: tag length limit, whether search returns it, whether uniqueness is enforced. |
| D6 | Size box | One big number (contracts) plus a small loud badge naming the binding source. Budget shown is **conservative remaining room after this trade** = min(daily loss room, drawdown buffer) minus open worst case. Refuse to stage if this trade's max loss > 25% of remaining budget; refuse any open if remaining < 2x max loss. Both fractions are config. |
| D7 | Fresh-state gate | Prefetch REST positions/orders when the ticket is STAGED (so place adds about zero latency); result valid 5 s. Re-check at place: user hub up, account/contract match, no unexpected position or order, active contract, quote age. **Quote age limit 2 s for opens, none for closes/flatten.** Reserve rate budget for emergency calls. |
| D8 | Qty cap and graduation | Guard-enforced `PX_MAX_QTY_ORDER=1`, per symbol 2, total 3, micros only, **sim account first**. Graduation is a checklist the operator acts on, not an auto-raise: 30 sim round trips with zero unreconciled UNKNOWN, zero orphan stops, flatten tested 5x including a forced network failure; then 20 clean eval trades at 1 micro; any UNKNOWN/orphan/manual override resets. Buy the funded account only after the sim stage is clean. |
| D9 | MCP | Read tools plus `preview_order` only in v1. No `place_ticket` registered at all while `PX_MCP_PLACE=0` (unknown-tool pattern). |
| D10 | Env surface | Keep to about 10 knobs; the rest are constants. Zero/unset limit means refuse. |

## Requirements added by round 2 (none of the round 1 designs had these)

1. **Trailing drawdown is the firm's number, not ours.** Model the floor conservatively (assume
   intraday unrealized peaks count), poll equity at 1 s or faster while a position is open, show the
   guard's number as an upper bound labelled "conservative budget". Prop firm unchosen, so rules are
   config until then.
2. **Flat-by deadline and session roll.** Block opens within N minutes of the configured cutoff,
   auto-flatten at T-minus, honour the 17:00 CT roll and holidays from a calendar, not the local clock.
3. **Voice-staging ambiguity.** Hard-bind one account id in config and refuse tickets for any other.
   Confirm screen shows SIM or LIVE, account suffix, exact contract id and expiry, side, qty, stop in
   ticks and dollars. Contract roll window blocks until set manually. A second ticket for the same
   intent within 60 s is blocked while any record is unreconciled.

## Build order (implementer's cut, adopted)

| Inc | Content | State |
|---|---|---|
| I1 | Phase 0 hardening: CT trade_date, trades seeded from REST, reseed race/failure handling, size-less position events, fail-closed account filter, no-retry `PXClient.write`, allowlisted `/state.json` and `/contracts`, fixed boot-error codes | **DONE** |
| I2 | `projectx_desk/limits.py` (persisted day state: trade date, order count, HWM, fail closed on None) + `projectx_desk/guard.py` preview/Ticket/hash/TTL/single-use + audit; no place() | **DONE** |
| I3 | `place()` + state machine (STAGED, PLACED(+acked), FILLED, CLOSED, REJECTED, EXPIRED, UNKNOWN), unique tag, fsync intent, reconcile, kill switch default off | **DONE** |
| I4 | Stop handling, flatten, halt, naked-position watchdog | **DONE** |
| I5 | Fresh-state gate, AST pin test, secret hygiene for MCP token | **DONE** |
| I6 | Read-only MCP + `/state.json` v1 + `#ai-digest` endpoint (one builder dict, two formatters) | **DONE** |
| I7 | Desk UI, POST routes with Origin required + per-session CSRF header, hold-to-confirm bar | **DONE** |
| I8 | Sim end-to-end rehearsal; doubles as Phase 1 live-read verification | needs the operator + credentials |

Built beyond the original cut: server-side quote pinning (so MCP/Codex work with no browser open), `stop_ticks` staging (server resolves the stop from the fresh quote), risk_pct applied to REMAINING budget (superseded in Round 4: now the drawdown room), `UNKNOWN_ORDER` visible in state/digest, `projectx_desk/dev_gateway.py` mock + e2e tests.

Deferred: MCP place, out-of-band confirm for live, automatic reconcile, SQLite capture, contract-roll drawings,
live-account cancel/flatten, trailing/multi-leg order types, Playwright schema test.
Over-built for a single local user (cut): per-class checks (use one function returning a list of
(name, ok, reason)), cross-process ticket atomicity (asyncio lock is enough), rotating CSRF, periodic
reconcile job.

## v1 "can place a sim order" checklist

- [ ] I1 merged, tests green
- [ ] Account is sim (config-asserted `PX_ACCOUNT_ID` + `PX_ACCOUNT_KIND=sim`); live ids refused unless `PX_ALLOW_LIVE=1`
- [ ] `PX_TRADING=1` set by hand; default 0 verified to refuse
- [ ] Order writes exist only in `projectx_desk/guard.py`; AST pin passes
- [ ] Ticket shows full readback, sized by `projectx_desk/sizing.py`, REFUSED on granularity
- [ ] Fresh-state gate passes; exactly one POST per place; ACK shown as ACK, not FILL
- [ ] Fill via user hub; stop after fill matches position qty; naked-watchdog tested
- [ ] Timeout/5xx gives UNKNOWN + halt + no retry; reconcile-by-tag resolves
- [ ] Flatten works on sim; halt works with `PX_TRADING=0`
- [ ] Daily count/loss persist across restart and fail closed on missing data
- [ ] Audit log has no token or account number
- [ ] Observed on the TopstepX UI: order, fill, stop, flatten

## Round 3: after independent reviews (2026-10-15)

Three independent model reviews (see `reviews/`) found real gaps. These are the changes made in response. Earlier rulings above
are kept as written; where one is superseded, this section says so. Still tested only against the mock gateway.

- **D3, fallback now opt-in.** The ruling above ends a failed flatten with a capped opposite market order. That order is now
  OFF by default (`PX_FLATTEN_FALLBACK=0`). A timed-out `closeContract` can still execute, so an automatic opposite order can flip
  the position. With the fallback off, an unconfirmed close halts and tells the human to close in the TopstepX UI.
- **Flatten verifies the book.** On sim it cancels working orders on the contract even with no position, then re-reads the book. A
  failed cancel is an orphan: `flat=False` and halt.
- **New check `contract_flat`.** One protection owner per net position: a second entry is refused while the contract has a position,
  a working order or an entry in flight. Scale-in is not supported.
- **Protection after restart.** `Guard.protect_open_positions()` runs after boot and every REST resync (known ticket arms or resizes,
  broker stop covers it, own intent without a provable stop flattens and halts, unknown positions are reported `EXTERNAL_UNPROTECTED`).
- **Budget.** Open stop risk of live tickets is subtracted from the remaining budget used for sizing.
- **Stops.** A resize after a partial fill re-sends the current stop price (never widens a tightened stop) and takes the stop lock. A stop
  that ACKs after the watchdog flattened is cancelled (sim) or reported as an orphan (live), and the desk halts. The watchdog depends
  on exposure, not on the entry reply state.
- **Commit point.** `place()` re-checks halt, ticket expiry and quote freshness synchronously after its awaited REST reads. A broker
  rejection carrying an `orderId` is treated as possible exposure: a position that then appears is protected and the desk halts.
- **Reads fail loudly.** `PXClient` read wrappers raise on `success:false` or a non-list payload instead of returning an empty list.
- **Losses.** Fills are de-duplicated by id and grouped by `orderId`, so one stop-out filled in pieces counts as one loss.
- **MCP.** Preview cannot supersede a ticket the human staged at the desk. `get_quote`, `get_bars` and `search_contracts` emit only
  finite numbers and re-emitted timestamps.
- **Intent resolve.** New human-only `POST /api/intent/resolve` (desk button "Resolve not-found") closes intents that reconcile marked
  NOT_FOUND. It has no MCP tool. Before, the desk stayed blocked until `data/intents.json` was deleted by hand.
- **Bind rule.** `create_app()` applies the same non-loopback refusal as `main()`.
- **Not fixed** is listed under "Known gaps" in `LIMITATIONS.md`.


## Round 4: the size base is the loss limit, not the balance or the remaining room

- **Tried:** `risk_pct` (default 3%) of the REMAINING budget, min(daily room, drawdown room). On a 50K Combine with a $500 daily
  limit that is $15 of 1R, so even one MES (about $40 at a 1.5 ATR stop) was refused as GRANULARITY. The desk could never trade.
- **Rejected:** Tharp's percent of account equity. A Combine balance is not the risk: the firm kills the account after a
  $2,000 loss (the MLL), so 1% of the $50,000 balance would be a quarter of the whole life of the account.
- **Went with:** 1R = `risk_pct` x live drawdown room (balance minus `PX_MLL_FLOOR`), capped by the remaining daily and drawdown
  room. Default 5% (chips 2/3/5/6): $100 while the room is $2,000. The stop (1.5 x ATR) sets the contract count as before.
  The room shrinks as you lose, so 1R shrinks with it. `PX_RISK_BASE=remaining` restores the old base.
- **Fails closed:** an unreadable balance, or no floor and no `PX_DRAWDOWN_BUFFER`, sizes to 0.
- **Kept:** micros only (`allowed_roots`), qty cap 1, the 25% max-loss-per-trade rule and the 2x remaining-budget rule (D6).
- **Open:** 5% is a hypothesis to review against the sim rehearsal, not a finding. `PX_DAILY_LOSS_LIMIT` is the operator's own
  number; the repo example of $500 is a starting point under Topstep's $1,000, not a recommendation.

## Round 5: which levels is price respecting, per timeframe

- **Ask:** instead of the trader picking EMAs or fibs by eye, scan each timeframe (5m, 15m, 1h, 4h, 1d) and draw only the levels
  price has actually reacted to. `projectx_desk/respect.py` (pure) runs the `projectx_desk.levels` bounce statistics over about 25 candidates per
  timeframe: EMA 8/13/21/34/55/89/200, SMA 20/50/200, Keltner, Bollinger, session VWAP, prior day or week high/low/close, swing fibs.
- **Selection bias is the enemy.** 25 candidates will always crown a lucky winner. So every winner shows `tested=K`, a level is only
  `stable` if it passes in both the earlier and the later half of the data, and anything under 30 tests carries SMALL_SAMPLE.
  The UI draws only levels with verdict ABOVE_RANDOM and dims the ones that are not stable.
- **Found and fixed a baseline bias.** The `projectx_desk.levels` placebo (lines shifted by fixed ATR offsets) is wrong for bands: on pure random
  walks Bollinger bands came out ABOVE_RANDOM in about 90% of runs, because bands sit at price extremes and the shifted placebo
  lines do not. `projectx_desk.respect` also scores each candidate on shuffled-return copies of the same bars and raises the placebo by any
  positive gap it shows there (it only ever raises the bar). Random walks then gave about 2% ABOVE_RANDOM and none stable.
  `projectx_desk/levels.py` itself is unchanged, so the existing `LEVEL:` lines still use the raw baseline. Known gap, see LIMITATIONS.
- **No lookahead:** a level's value on bar i uses bars up to i only; pivots for fibs count only after the lookback has confirmed
  them. Tests pin both.
- **Cost:** `GET /api/respect` and the digest `RESPECT:` block share one cache keyed on the last closed bar. A cold start is 5
  `retrieveBars` calls against a 50 per 30 s budget; steady state is one 5m refetch per 5 minutes. The AI surface carries only
  allowlisted ids, verdict enums and finite numbers; `get_respect` is read-only.
- **Not a forecast.** It says how price reacted, never where it will go. The UI and the digest both say so.
- **UI.** The chart draws the top ABOVE_RANDOM level per timeframe (solid when stable, dashed when not) and a "Respected levels"
  panel lists the top three per timeframe with held/tests, hold rate against placebo, SMALL SAMPLE under 30 tests and K tested.
  A "Timeframes" panel shows 5m, 15m and 1h mini charts. Its bias line scores the full EMA ribbon (8/13/21/34/55/89), not
  8/21/34: on NVDA the 3-EMA stack called a trend 38 to 49% of the time and the full ribbon 30 to 37%, so the short stack is
  the looser signal. A cold page load costs 3 extra `/bars` calls (250 bars each).
- **First evidence (NVDA, 5y daily, ~3 months of 1h, ~1 month of 15m).** On daily, no EMA from 8 to 89 beat its placebo and the
  hold rate fell with length (66% at 8, 39% at 89). On 1h and 15m the longer averages looked best among the MAs but not
  significantly (SMA 50 on 1h: 9 of 12, stable, small sample). One ticker, small samples: a prompt to run it across a basket
  of megacaps and the index futures, not a finding.
- **Basket check (10 tickers: SPY, QQQ, NVDA, AAPL, MSFT, AMZN, GOOGL, META, AVGO, TSLA; 5y daily, 6 months of 1h, 1 month of 15m).**
  No EMA length is reliably respected. Hits per timeframe vs luck (about 2% of candidates): 8 vs 4.8 on daily, 5 vs 5.0 on 1h, 12 vs 5.0
  on 15m; zero long-EMA hits stable on any timeframe. The one consistent signal runs the other way: EMA 8 reads 17 to 21 points
  below its placebo on every timeframe, and 8/13/21/34 average -7 to -9 points of edge against about -1 to +0.5 for 55/89/200. Price
  slices through the fast EMAs more often than through a shifted line, and the long EMAs are roughly neutral. The ten tickers are
  correlated and tests per level are small, so this is a prompt, not a finding.

## Round 6: level-anchored stops, GEX levels, the trade tape, tick bars (2026-10-16)

Built by three parallel builders, then reviewed read-only by two other models (one on the risk rules and order path, one on hostile
data, concurrency and persistence). Their findings are folded in below; nothing here touches `projectx_desk/guard.py` or the AST pin.

- **Level-anchored stop and target (`projectx_desk/anchor.py`, wired in `projectx_desk.digest.size_idea`).** This amends Round 4's "the stop (1.5 x ATR)
  sets the contract count": on the default 1.5x chip only, if a *respected* level (scanner verdict ABOVE_RANDOM, never GEX) lies
  adverse to entry between 1.5x and 2.0x ATR away, the stop tucks just beyond it (level -/+ max(2 ticks, 0.10 ATR)); the stop is never
  tighter than 1.5x ATR and never wider than 2.0x. The anchor is used only if it stays within `PX_MAX_STOP_TICKS` and within the risk
  budget, otherwise the plain ATR stop stands, so it cannot create a refusal the base stop would not have had. At the qty cap of 1 a
  wider stop means more dollars, not fewer contracts (bounded by the budget). The 1.0x/2.0x chips and tick steps are manual and bypass
  the anchor (a manual step never lands exactly on 1.5: the UI nudges it to 1.5001). The UI still stages by tick distance, so the
  "beyond level X" wording is true as of the last refresh, not necessarily at fill. Target: nearest respected favorable level at
  >= 1R, shown with R:R and break-even win rate; none shown if no level qualifies. The size box also shows drawdown and daily room
  left if the stop fills (netted for open stop risk) and total open risk with this trade.
- **GEX levels (`projectx_desk/gex.py`) are display only.** Fetched from the TraderDaddy public API (`PX_GEX_KEY`, fallback `TD_API_KEY`;
  `PX_GEX_URL`; `PX_GEX_MAX_AGE_S` default 900), converted with `projectx_desk/gex_futures.py`, drawn by the existing GEX overlay and listed in the
  digest. They never feed sizing, stops or targets: "GEX marks positioning, not targets" stands. Only validated numbers and fixed
  enums reach the AI surface; the source's free text is dropped. Reads are streamed with a hard 2 MB cap.
- **Partial quote merge (`projectx_desk.state._on_quote`).** The gateway sends only changed fields, and replacing the quote blanked sizing. The
  quote now merges, but `_rx` then means "something arrived", so the whole quote is cleared on any hub resync/down and when quotes are
  unsubscribed, and a crossed book keeps only what just arrived. The guard's 2 s freshness rule is unchanged: nothing can outlive an
  outage and look fresh. (Review finding: without the clearing, a stale ask could have sized a stop 3.5x too far.)
- **The trade tape is recorded whether or not a browser is open (`projectx_desk/flow.py`, `projectx_desk/ticks.py`).** Real buy/sell volume comes from the
  aggressor side on each trade (`GatewayTrade.type`: 0 buy, 1 sell). The tape stays subscribed for up to 4 contracts after the tab
  closes, and any `ensure_quote` starts it. There is no tick history, so coverage starts when recording starts. Per-minute buckets
  persist to `data/flow.json` (`PX_FLOW_FILE`; gitignored, numbers only) every 30 s; a restart, a reconnect, a tape eviction or an
  unusable timestamp becomes a recorded *hole*, and any bar or digest window overlapping a hole is blanked or marked INCOMPLETE rather
  than shown as a wrong number. The file is re-validated on load (types, ranges, caps). Volume of unknown side is counted apart.
- **Tick bars (`projectx_desk/ticks.py`, `PX_TICK_BARS`, default 1024,2048,4096 trade prints per bar).** ProjectX has no tick unit, so these are
  built from the tape only, in memory, and restart empty. Each bar carries buy and sell volume; bars that missed prints are flagged.
- **Known limits.** The anchor can flip between ANCHORED and ATR as price moves relative to fixed levels. Whether the gateway re-sends
  recent trades on a resubscribe (which would double count) is unverified. `live: true` bars are refused for this account, so the
  digest and chart use the sim data subscription, whose bars were checked identical to the live tape (see LIMITATIONS).

## Round 7: Codex second opinion, all seven findings reproduced and fixed (2026-10-16)

`reviews/review-codex-2026-10-16.md` reviewed `db1be93`. Its embedded reproduction script was run first; every finding reproduced exactly.

- **A vanished stop no longer makes us forget our own position (P1).** `protect_open_positions` used to retire an owned ticket whose
  stop was gone from the broker book and then call the still-open position `EXTERNAL_UNPROTECTED`, untouched. It now decides by
  evidence, in order: (1) another broker stop already covers the position (the trader replaced ours): report COVERED, touch nothing;
  (2) our stop FILLED (a non-voided fill carrying its order id is on the trade list): the position we owned is gone, so a position
  now present is new and external, never touched; (3) neither: the stop was cancelled or lost, the position is still ours and naked,
  so flatten and halt (no halt if the flatten finds it already gone). The broker position id is recorded on the ticket but is NOT used
  as evidence: whether the real gateway keeps one id per position is unverified (an independent review caught this: with an unstable
  id our own naked position would have been called external). A missing stop alone is never a reason to assume a hand entry.
- **A stop ACK after an ordinary close is no longer left working (P1), without false halts.** A position-closed event marks an
  in-flight stop `abandoned`, so its late ACK is cancelled. An already ACKED stop whose fill is on the tape, or that the hub no longer
  lists, needs no broker call (a normal stop-out). Otherwise the REST book is read with retries (a just-filled stop can linger for a
  beat, and one failed read must not halt a normal exit); a stop that is still working is cancelled, and a cancel the book confirms
  finishes quietly (a manual close on sim is not an incident). Only an unconfirmed cancel (orphan: the stop intent is kept as ORPHAN)
  or an unreadable book after every retry halts. The watchdog's late-stop path still always halts. Assumption: `live.orders` is
  hub-fed and resynced from REST, so a stop absent from it is treated as gone.
- **`place()` re-validates everything at the commit point (P1, P2).** One synchronous `_commit_gate` after the last await checks the
  ticket is still STAGED (a restage during placement used to be overwritten and the superseded ticket sent), halt, expiry, user-hub
  health and seeding, quote freshness, market drift in ticks, and risk headroom (including the budget multiple `evaluate` uses).
- **Flatten always re-reads the book after cancelling (P1).** An initially empty snapshot used to report flat with no further read.
- **Stream mode redacts the new dollar fields (P2).** `drawdown_left`, `daily_left`, `heat_usd` and `heat_pct` are removed from the
  digest and state, the UI shows "hidden in stream mode", and the page now passes `stream` to `/state.json`.
- **An ongoing tape outage no longer ages into a quiet window (P2).** `FlowStore.snapshot` derives an open hole from the last trade to
  now while a gap is flagged, so every overlapping bar blanks and every digest window is INCOMPLETE until trades resume.
- **Dev isolation and docs.** `run_dev.sh` sets `PX_NO_ENV_FILE=1`, unsets the advisory feed/GEX variables and uses its own tape file;
  `docs/LIMITATIONS.md` now separates verified read behavior from unverified order writes and no longer claims GEX is always off.
- **Not done:** a rendered browser pass of stream-mode DOM transitions, and the operator-led real sim rehearsal.

## Round 8: independent bug hunts after the Codex fixes (2026-10-16)

Three more read-only passes (an attack on the Codex fixes, a fresh-eyes hunt across the repo, and a fuzz run over the pure modules).
Everything below was reproduced before it was fixed and has a regression test.

- **An unrecognised position side on our contract is flattened, not skipped.** `handle_position` used to `continue` past a position
  whose `type` it did not map, which (with the Short=2 enum still unverified) left a real fill with no stop and no watchdog while
  `protect_open_positions` reported it `TICKET`. It now flattens and halts (`position_side_unknown`), and `protect_open_positions`
  only reports a ticket handled if its stop is armed or in flight.
- **The desk refuses to boot onto an arbitrary account** when `PX_ACCOUNT_ID` is blank (opens already refused; flatten, the watchdog
  and `PX_FLAT_BY` would have acted on whichever account came first). `PX_FLAT_BY=17:00` is refused like any garbled value (the
  window ends at the session open, so it could never fire).
- **Stream mode hides the percent fields too** (`pct_budget`, the ticket's `pct_of_budget`); `daily_used_pct` is in 10% buckets.
  With contracts and stop ticks visible, exact percentages let a viewer back out the loss room and the day's P&L.
- **STAGE refuses a size box computed for different chips.** The click handler now compares the contract, side, stop and risk the
  size was computed for with what the chips show now (a stale 150-tick MNQ stop could have been staged for MES).
- **Exact tick arithmetic in the size box.** Float drift added a phantom tick on 0.01 and 0.1 tick contracts (MCL, MGC, M2K): the
  box showed REFUSED where the guard sized one contract. The box also flags `STOP_DISTANCE` for a stop wider than
  `PX_MAX_STOP_TICKS` instead of showing OK and letting STAGE refuse.
- **Sizing hardening (fuzz pass).** Out-of-range or overflowing inputs raise `SizingError` (never `Decimal`/`Overflow` crashes), a
  stop must be a whole number of ticks, and `describe_trade` never sizes beyond the remaining budget (the MCP size tool could).
  A bad print leaves a partial minute even with no bucket, so its window reads INCOMPLETE; fractional volume is a bad print.
- **Known and accepted.** The respect scanner calibrates its placebo on all bars including later ones by design (a whole-sample
  statistic): treat `scan()` as no-lookahead per level value, not per verdict.

## Round 9: keep the data we have (2026-10-16)

- **GEX serves the last good levels when a refresh fails.** The API key is shared and limited to 30 requests a minute; a 429 used to blank
  the chart and flip the source to SPY, whose converted flip sits about 90 points from SPX's. A failed refresh now keeps the last good
  payload from the same source (retry after the 60 s error TTL). Its age keeps counting, so `PX_GEX_MAX_AGE_S` still bounds it; an auth
  failure is never masked.
- **A tape blip of 15 s or less no longer blanks a bar** (`SHORT_GAP_MS` in `flow.py`). A reconnect that resumes that fast loses a few
  percent of one bar's trades, and blanking the whole bar hid good data. Longer outages are still holes, and an outage still in progress
  shows as an open hole once it passes 15 s. Gap flagging is decided when trades resume, not when the hub drops.
- **The desk runs as a systemd user service** so closing a terminal or an editor session no longer creates unrecoverable tape holes
  (`docs/DEPLOY.md`), and a clean shutdown saves the tape.
- **Not changed, on purpose:** the bracket mode. Native OCO on entry needs the TopstepX account's own bracket setting to be Auto OCO
  (the API cannot read it), and a wrong `PX_BRACKET_MODE` creates an order the gateway rejects but still books. It stays an explicit,
  human-set value, to be proven with one watched sim order.
- **GRANULARITY refusals on volatile days were the ATR scaler, not the rule.** At ATR in the 99th percentile the 1.5x ATR stop already cost $80 per
  MES, and the untested x0.67 cut then shrank a $100 budget to $67, so one contract could never fit. The "never round up" rule is unchanged; the
  owner's config now sets `PX_SIZE_SCALERS=0` (documented in `docs/SIZING.md`). The scalers stay available for anyone who wants them (`PX_SIZE_SCALERS=1`).
  **Superseded by Round 10:** the default is now off.

## Round 10: the statistics review (2026-10-17)

An outside review (`reviews/review-statistics-2026-10-17.md`) graded the repo as a cost-control layer plus one research-shaped
module, and found the problems were statistical, not engineering. Each claim was checked against the code before anything moved.
What it got right, what it had stale, and what changed:

- **Right: `min_n=8` with a 15-point margin was 0.87 standard errors.** At p = 0.6 and n = 8 the standard error of a proportion is
  17 points, so one candidate in five clears a 15-point gap on pure noise, and the scan tests ~125 of them. The surrogate calibration
  (Round 5) already pulled the measured false-positive rate on random walks to 2 of 600 rows, which the review did not account for, but
  calibration is not a significance test. A level is now RESPECTED only when three gates pass: (1) at least 30 resolved tests and a
  15-point gap over the calibrated placebo (`levels.MIN_TESTS`, the effect-size gate), (2) a one-sided exact binomial p-value of that
  many bounces against the placebo rate (`levels.binom_sf`), (3) that p-value survives a Benjamini-Hochberg cut at q = 0.10 over every
  candidate on every timeframe in the scan (`levels.benjamini_hochberg`, `respect.FDR_Q`; the family size is reported as `family`).
  A gap that clears the margin but not the FDR cut reads NOT_DISTINGUISHABLE. Rows carry `p_value` and `fdr` so a reader can see
  which gate failed. The stability halves need 15 tests each (was 5). Measured on 600 null rows (Gaussian and volatility-switching
  walks) the new gates cost nothing: 2 and 0 false RESPECTED before, 2 and 0 after. They bind on real data, where the calibration can
  under-correct, and that is the point. `anchor.py` inherits the stricter verdict, so fewer stops are anchored; the base ATR stop stands.
- **Right in spirit on the placebo, done differently.** The review asked for a 1000-draw circular block bootstrap so the verdict becomes
  a p-value. The scan runs live in pure Python on five timeframes; 1000 draws is minutes, not milliseconds. The surrogates ARE now a
  circular block bootstrap of the bar shapes (blocks of 20 bars, `respect.BLOCK`), so volatility clustering survives in the null while
  the tie between a line and the bars after it is broken, but there are still three of them and they calibrate the placebo rate. The
  p-value comes from the exact binomial against that calibrated rate, not from the surrogate distribution. If the scan ever moves out
  of the request path, raise `SURRG` and derive the p-value from the surrogates directly.
- **Not changed, on purpose: the wick-versus-close asymmetry.** BOUNCE is judged on a wick at 0.75 ATR, FAIL on a close at 0.25 ATR.
  The review is right that the hold RATE drifts with the volatility regime for mechanical reasons. The placebo lines are scored with
  the identical rules at the same bars, so whatever the asymmetry does to the hold rate it does to the placebo rate; the gap and the
  p-value compare like with like. Symmetrising would change what "held" means on every chart and every test fixture for a bias the
  design already cancels to first order. Noted in the module docstring.
- **Right: the ATR scaler prices volatility twice.** `contracts = risk / (k x ATR x tick value)` already makes exposure proportional
  to 1/ATR; a further x0.67 above the 80th percentile makes it roughly ATR^-1.4. Round 9 admitted this and left the default on.
  `PX_SIZE_SCALERS` now defaults to OFF; `1` turns both cuts on. Nothing else about them changed: they still only ever reduce.
- **Right: cost was shown nowhere.** On micros a 15-tick MES stop risks $18.75 against about $3.50 of commission and two ticks of
  slippage, so 1:1 breaks even at 59% wins, not 50%. The size box, `/ai/state` and the digest now carry `SIZE_COST`: cost per contract
  per round turn, that cost as a percentage of 1R, and the win rate 1:1 needs after costs. Inputs: `PX_COST_RT_USD` (your fees per
  contract per round turn; unset means the cost prints NA, never 0) and `PX_SLIPPAGE_TICKS` (default 2, one in and one out). The
  KPI block adds `KPI_COST`: fees actually paid today and fees as a share of gross P&L moved. Stream mode hides the dollar figures
  and keeps the percentages.
- **Right, and the most important one: the room ignored open P&L.** Topstep watches the maximum-loss limit on equity including
  unrealised loss; the balance the gateway reports moves on fills. The room was live balance minus the floor, less the stop risk of
  our own live tickets, so one large open loser inside its stop (or an external position with no stop at all) could liquidate the
  account while the digest read `locked=NO`. The review was stale on one point: with `PX_MLL_FLOOR` the room already followed the
  live balance, it was never only a typed number. Now `Guard.open_pnl()` marks every open position at the last trade of a quote no
  older than the fixed 30 s `max_hub_age_s` (not an env variable), using the contract's tick size and value; `Guard.draw()` is the WORSE of the committed stop risk of
  our tickets and the open loss showing (a gain is never banked); both rooms are net of it. If any open position cannot be valued (no
  fresh quote, no contract record, a PositionType other than 1 or 2) the room is UNKNOWN and opens refuse with `open_pnl_known`, the
  same way an unknown day P&L refuses. `RISK_OPEN` carries `open_pnl` and `draw`; stream mode hides both. Assumption, unverified
  until a sim run: the gateway's `balance` excludes open P&L (the mock gateway behaves that way). If a real account's balance turns
  out to include it, the open loss is counted twice, which errs small, and the fix is one line in `draw()`.
- **Right: the analyst feed was the lowest-friction entry on the screen.** Every statistical control pointed at levels and none at
  the one third-party signal with a STAGE button. The one-click STAGE on a callout now sits behind `PX_FEED_STAGE=1` (default off:
  the banner, the chart switch and the drawn levels stay; staging the idea takes the same typing as any other). When it is on, a
  ticket staged from a callout writes a `stage_source=callout` audit event, so callout-origin tickets can be measured apart. The
  backtest the review asked for (100+ historical ENTRY posts net of costs) needs history the desk does not store; nothing here
  pretends to answer it.
- **Not actionable here:** the invalidation tests for the behavioral rails and the scalers (200+ logged trades, KS tests on R after a
  loss versus after a win) need a trade log this desk does not yet have. The audit file now records enough (ticket, risk, source) to
  build one. The "mocks encode the author's assumptions" point stands and is why nothing has been pointed at a real account.

## Round 11: manual BUY / SELL, and the chart palette (2026-10-18)

- **The size box advises; it no longer gates.** STAGE only works when the ATR sizing says OK, so a GRANULARITY refusal (one contract
  risks more than the scaled budget) or a missing ATR left the trader with no way to enter at all. The Ticket panel now has BUY and
  SELL with a contract count and a stop distance in ticks (the stop mirrors the ATR stop until you type your own; ATR snaps it back).
  It is NOT a second order path: the button posts `qty` to the same `/api/preview`, the guard derives the risk (`qty x stop ticks x
  tick value`, exact decimal so the floor lands on `qty`) and runs every check unchanged: halt, account, quote age, stop side and
  distance, session window, flat-by, the loss room net of open P&L, the contract caps, the behavioral rails, `contract_flat`,
  `no_unknown`. Two checks are added: `manual_qty_cap` (above `PX_MAX_QTY_ORDER`) and `manual_qty` (the room, the after-loss cap or
  the caps allow fewer than asked: refused with the number they allow, never silently reduced). The ticket readback says
  `sized_by=MANUAL`, the audit writes a `manual` event, and placing is the same hold. Exits are still FLATTEN and the stop buttons.
  MCP gets no `qty`: the AI surface sizes by risk only, the human picks a size by hand.
- **What still stops you, and the knobs:** the cooldown and lockout (`PX_COOLDOWN_AFTER_LOSS_S=0`, `PX_MAX_CONSEC_LOSSES=0` turn them
  off), the after-loss size cap (`PX_SIZE_AFTER_LOSS_LOCK=0`), the daily loss limit and `PX_MLL_FLOOR` (not negotiable, Topstep ends
  the account), the per-order cap (`PX_MAX_QTY_ORDER`) and one open trade per contract. None of these were loosened.
- **The TraderMatrix chart-card palette**, taken from the chart engine's constants rather than from pixels: gold `#D6A73A` up candles,
  steel `#7088AD` down candles with light `#C8D8EE` wicks, card `#0A0E17` on brand navy `#070E1F`, panel `#0F1522`, ink `#F2F5FA`,
  the EMA-21 blue `#5FB3D9` as the desk's accent, lilac `#B49AE0` for the third line, and the card's text tones (`#8EA3C4`, `#6C7A92`,
  `#56637A`). `C` in `desk.js` and the `:root` tokens in `desk.css` carry them; the volume bars, buy/sell volume pane, tick bars and
  mini-charts use the same up/down pair. Two golds exist: the brand kit's `#D4A62A` and the card's `#D6A73A`; the desk is a chart, so
  it uses the card column. Green and magenta stay for outcomes (held/failed, stop lines, badges): a colour that means "price went up"
  must not also mean "good". BUY is gold, SELL is steel.

## Round 12: the 0DTE scalper teardown, checked against the desk (2026-10-18)

Source: the combined blueprint of a public SPY 0DTE scalping desk (a teardown of a third party's dashboard plus the author's article;
screenshot values and the author's performance claims are unverified, 3 of a planned 10 forward sessions). It is an options desk on a
poll-only broker; this desk trades futures on a streaming hub, so only the safety and cost lessons transfer. Checked item by item:

- **Already here:** the stale-quote gate that fails closed on a missing timestamp (`quote`, re-checked at the commit point); never
  replay a write, ambiguity becomes `UNKNOWN`, persist, halt, reconcile; ACK is not a fill; boot with trading off (`PX_TRADING=0`);
  the cost line and break-even win rate (`SIZE_COST`, `KPI_COST`, `PX_COST_RT_USD`, `PX_SLIPPAGE_TICKS`); a sample size `n` beside
  every KPI; a stop that only ever tightens.
- **Added: the exclusive process lock** (`projectx_desk/lock.py`). The blueprint's rule "two engines on one account is a bad day" had no
  counterpart here: two desks on one checkout would each run a watchdog and stop owner and could place the same trade twice. Taken in
  `main()` only, kernel-released on exit or crash, an unopenable lock file refuses to start (never guess). It is per checkout, not per
  account (see LIMITATIONS).
- **Not added, and why:** the execution-quality log (quote age, submit-to-ACK latency, signed slippage) would touch the `place` path and
  its audit allowlist; it is recorded as a known gap, not rushed into a release. The regime gate, delta-band scout, mean ladder and
  bracket tree are options-specific and do not apply to a one-contract futures desk. The "trail read the future" and float-spread
  harness bugs apply to a backtester this desk does not have; if one is built, keep prices as integer ticks and fill a stop only on a
  later observation than the one that set it.
- **A claim to distrust, from the same source:** a 94% win rate at a 3-tick target against a 25-tick stop needs 88.9% just to break
  even; the live panel in the same screenshots ran at 75% and a negative expectancy. The desk's own cost and break-even lines exist to
  show that arithmetic before a trade, not after.

## Round 13: a calmer desk, same layout (2026-10-18)

The owner liked the chart-first, low-noise look of a reference 0DTE scalper dashboard (see Round 12). The layout is unchanged (three
columns, same panels); only the density came down, in `index.html`, `desk.css` and `desk.js`. No server code, no id or `data-ai`
binding, no POST, no hold, halt or flatten control changed.

- **KPI ribbon:** a small title, one big value, at most one quiet sub line. Tag text moved to tooltips; the progress meters and the
  wins/losses bar are hidden (elements kept); the number takes the tone instead of a coloured card edge. The win-rate card keeps
  `need X%` and `n`, and says "small sample" under 30 trades: sample size stays on screen.
- **Panels:** sentence-case titles, one chip at a time in the size box (lock, then scale, then cap; the rest stay in the WHY lines), the
  prose math line hidden (its values stay in the DOM), no glows or gradients, hairline borders, three type sizes (11, 12, 15).
- **Level reactions and Respected levels:** the SMALL SAMPLE, TOO FEW TESTS, STABLE and verdict chips became one muted line per row,
  colour carrying the state. The verdict and every sample size are still shown.
- **Kept loud on purpose:** state enums (TRADING ON, NOT ALLOWED, REFUSED, UNPROTECTED), HALT, FLATTEN, BUY and SELL.
- `docs/img/*.png` and `demo.gif` regenerated from the offline mock desk by `scripts/screenshots.py` (Playwright takes the shots; the gif step needs Pillow, so install `requirements-screenshots.txt` into the same venv as Playwright).

