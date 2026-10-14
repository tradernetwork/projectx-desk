# Design decisions (rounds 1 and 2, 2026-10-14)

Six agents (guard designer, red team, trader UX; then trader's advocate, risk engineer,
implementer) argued the contested points. This is the reconciled ruling. Where they
disagreed, the reasoning for the pick is given. Items marked UNVERIFIED must be proven
against a sim account before they are relied on.

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
| I2 | `px_limits.py` (persisted day state: trade date, order count, HWM, fail closed on None) + `px_guard.py` preview/Ticket/hash/TTL/single-use + audit; no place() | **DONE** |
| I3 | `place()` + state machine (STAGED, PLACED(+acked), FILLED, CLOSED, REJECTED, EXPIRED, UNKNOWN), unique tag, fsync intent, reconcile, kill switch default off | **DONE** |
| I4 | Stop handling, flatten, halt, naked-position watchdog | **DONE** |
| I5 | Fresh-state gate, AST pin test, secret hygiene for MCP token | **DONE** |
| I6 | Read-only MCP + `/state.json` v1 + `#ai-digest` endpoint (one builder dict, two formatters) | **DONE** |
| I7 | Desk UI, POST routes with Origin required + per-session CSRF header, hold-to-confirm bar | **DONE** |
| I8 | Sim end-to-end rehearsal; doubles as Phase 1 live-read verification | needs the operator + credentials |

Built beyond the original cut: server-side quote pinning (so MCP/Codex work with no browser open), `stop_ticks` staging (server resolves the stop from the fresh quote), risk_pct applied to REMAINING budget, `UNKNOWN_ORDER` visible in state/digest, `dev_gateway.py` mock + e2e tests.

Deferred: MCP place, stream-mode DOM removal (3b), SQLite capture, contract-roll drawings,
live-account cancel/flatten, trailing/multi-leg order types, Playwright schema test.
Over-built for a single local user (cut): per-class checks (use one function returning a list of
(name, ok, reason)), cross-process ticket atomicity (asyncio lock is enough), rotating CSRF, periodic
reconcile job.

## v1 "can place a sim order" checklist

- [ ] I1 merged, tests green
- [ ] Account is sim (config-asserted `PX_ACCOUNT_ID` + `PX_ACCOUNT_KIND=sim`); live ids refused unless `PX_ALLOW_LIVE=1`
- [ ] `PX_TRADING=1` set by hand; default 0 verified to refuse
- [ ] Order writes exist only in `px_guard.py`; AST pin passes
- [ ] Ticket shows full readback, sized by `sizing.py`, REFUSED on granularity
- [ ] Fresh-state gate passes; exactly one POST per place; ACK shown as ACK, not FILL
- [ ] Fill via user hub; stop after fill matches position qty; naked-watchdog tested
- [ ] Timeout/5xx gives UNKNOWN + halt + no retry; reconcile-by-tag resolves
- [ ] Flatten works on sim; halt works with `PX_TRADING=0`
- [ ] Daily count/loss persist across restart and fail closed on missing data
- [ ] Audit log has no token or account number
- [ ] Observed on the TopstepX UI: order, fill, stop, flatten
