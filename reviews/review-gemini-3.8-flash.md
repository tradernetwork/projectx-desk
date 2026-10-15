# Review by gemini-3.8-flash, 2026-10-14

## Verdict
Ship to sim rehearsal, but only after fixing the naked position on hub reconnect race.
The three deciding factors:
1. The mechanical AST order-write pin and fail-closed limit architecture are exceptionally disciplined.
2. If the user hub drops during a fill, the position is left completely naked with no watchdog running.
3. Every order and cancel behavior on TopstepX remains unverified until proven on a live sim account.

## Blueprint vs built
| Item | Status | Evidence (file:line) | Better or worse than blueprint |
|---|---|---|---|
| D1 Place control | Built differently | `static/desk.js:685`, `px_guard.py:563`, `px_app.py:280` | Weaker: 600 ms UI hold is built, but out-of-band confirm code and 3s stage-to-place gate were not built; `confirm_hash` is just `ticket.hash8` returned directly by `/api/preview`. |
| D2 Brackets | Built as designed | `px_limits.py:54,102`, `px_guard.py:412,489,720` | As designed: unset mode refuses opens; `position` mode arms stop after fill with 5s watchdog; live requires `oco` and market entry. |
| D3 UNKNOWN / missed stop | Built differently | `px_guard.py:544,772,835`, `px_client.py:137` | Stronger: auto-clear was deliberately not built, so an ambiguous write requires human intervention and cannot auto-resume into a bad state. |
| D4 Stream mode | Built as designed | `px_digest.py:352`, `px_app.py:205`, `static/desk.js:64` | As designed for DOM removal and AI redaction, but weaker on `/api/preview` which still returns dollar amounts and account suffix over HTTP. |
| D5 customTag | Built differently | `px_guard.py:497,597,637`, `px_app.py:350,405` | Weaker on startup: reconcile is manual via `POST /api/reconcile` and does not auto-run on boot or UNKNOWN. Safer against runaway API loops. |
| D6 Size box | Built differently | `px_limits.py:66`, `px_guard.py:274,443,455`, `sizing.py:246` | Weaker: sizes down to budget multiple and 25% max loss, but `budget()` does not subtract the worst-case risk of already open positions. |
| D7 Fresh-state gate | Built differently | `px_guard.py:391,530,587` | Stronger for safety, slightly worse for latency: stage prefetch was not built; REST snapshot is queried synchronously at place time inside the place lock. |
| D8 Qty cap and graduation | Built as designed | `px_limits.py:55-58`, `px_guard.py:440-442` | As designed: 1 contract per order, 2 per symbol, 3 total, micros only. Graduation remains an operator checklist. |
| D9 MCP | Built as designed | `px_mcp.py:78-197`, `tests/test_px_mcp.py:20` | Stronger: no `place` tool is registered in the tool schema at all, rather than refusing at runtime. |
| D10 Env surface | Built as designed | `px_limits.py:48-116`, `.env.projectx.example:1-51` | As designed: 15-20 core knobs; zero or blank money/quantity limits fail closed; behavioral rails default safe unless explicitly set to 0. |
| R2-1 Trailing drawdown is firm number | Built differently | `px_limits.py:65`, `px_guard.py:274-282`, `docs/LIMITATIONS.md:36` | Weaker: 1s equity polling and unrealized intraday peak tracking were not built; `dd_buffer` is a static number typed into config. |
| R2-2 Flat-by deadline and session roll | Built differently | `px_guard.py:256-271`, `px_time.py:18-22` | Weaker: entry cutoff blocks opens after deadline, but auto-flatten at T-minus and holiday calendar were not built. |
| R2-3 Voice-staging ambiguity | Built differently | `px_guard.py:359,434,480,500` | As designed on single-account binding and readback; stronger on blocking all opens while any intent is unresolved; not built on contract roll window lock. |
| Checklist 1: I1 merged, tests green | Built as designed | `tests/conftest.py`, pytest suite | 312 tests pass offline against mock gateway. |
| Checklist 2: Account is sim | Built as designed | `px_guard.py:359-366` | Live accounts refused unless `PX_ALLOW_LIVE=1` and `PX_ACCOUNT_KIND=live`. |
| Checklist 3: PX_TRADING=1 set by hand | Built as designed | `px_limits.py:97`, `px_guard.py:520,572` | Defaults to 0; place returns error if disabled. |
| Checklist 4: Order writes in px_guard only | Built as designed | `tests/test_px_guard_pin.py:61` | AST pin verifies no other module calls `write` or names order write endpoints. |
| Checklist 5: Ticket readback and granularity | Built as designed | `px_guard.py:484-517`, `sizing.py:279` | Sizing always floors; 0 contracts results in `REFUSED (granularity)`. |
| Checklist 6: Fresh-state gate, 1 POST, ACK | Built as designed | `px_guard.py:530-542,606,627` | Synchronous REST verification; single POST attempt; returns ACK not FILL. |
| Checklist 7: Fill via user hub, stop, watchdog | Built as designed | `px_guard.py:691-776` | Fill triggers stop submission; 5s watchdog flattens and halts on missing ACK. |
| Checklist 8: Timeout gives UNKNOWN, halt | Built as designed | `px_guard.py:607-617,637-668` | `PXWriteUnknown` trips halt, writes persistent intent, reconcile searches by tag. |
| Checklist 9: Flatten on sim, halt works | Built as designed | `px_guard.py:806-865` | Flatten closes positions and verifies flat; works even with `PX_TRADING=0`. |
| Checklist 10: Daily count/loss persist | Built as designed | `px_limits.py:137-176` | `DayState` persists to disk with atomic write and fsync. |
| Checklist 11: Audit log clean | Built as designed | `px_guard.py:210-225` | Strict allowlist regex: no tokens, balances, or free-form gateway strings. |
| Checklist 12: Observed on TopstepX UI | Not verified | `docs/LIMITATIONS.md:7` | Cannot be verified on mock gateway; requires real sim rehearsal. |

## Findings
### F1 Unprotected naked position on user hub drop or reconnect  [severity: critical]  [confidence: verified]
- Where: `px_guard.py:680-720`, `px_state.py:181-192`, `px_app.py:421-446`.
- Failure sequence:
  1. Operator stages and places an entry order in `position` bracket mode.
  2. The entry order is accepted by the gateway (`t.state = PLACED, t.acked = True`).
  3. The WebSocket connection for `user_hub` drops or resets while the entry order is resting or filling.
  4. The fill occurs on the broker, but the `GatewayUserPosition` event is never received by `LiveState`.
  5. Because `_watchdog(t)` is spawned exclusively inside `_arm_stop(t, size)` (`px_guard.py:720`) rather than in `place()`, no watchdog is running.
  6. When `user_hub` reconnects, `_schedule_reseed` runs and calls `live.seed(positions, orders)` (`px_state.py:181`).
  7. `live.seed()` updates internal position dictionaries, but it never calls `_emit("position", d)` or notifies the guard.
  8. The guard never invokes `handle_position()`.
  9. Result: The position is open on the broker with no stop loss and no watchdog timer. The desk remains completely oblivious until manually flattened.
- Fix:
  1. Start the watchdog timer inside `place()` immediately upon order acknowledgment, not inside `_arm_stop()`.
  2. In `LiveState.seed()`, iterate over seeded positions and emit events for any active position, or have `_reseed()` explicitly call a guard reconciliation hook `guard.sync_open_positions()`.
  Missing test: In `tests/test_px_guard_stop.py`, simulate an entry fill arriving via REST snapshot rather than SignalR event, and assert that `_arm_stop` is invoked within 1 second.

### F2 Deadlock in operator recovery when intent is NOT_FOUND  [severity: high]  [confidence: verified]
- Where: `px_guard.py:45, 434-436, 665-674`, `px_app.py:350-354`.
- Failure sequence:
  1. An entry order POST times out or returns a network error, entering `UNKNOWN` state and writing an intent record.
  2. The operator clicks "Reconcile" on the desk (`POST /api/reconcile`).
  3. The broker order search confirms the order was never received by the broker.
  4. `reconcile()` transitions the intent state to `NOT_FOUND` (`px_guard.py:665`).
  5. In `px_guard.py:45`, `PENDING_INTENT` is defined as `("SENDING", "UNKNOWN", "NOT_FOUND")`.
  6. The operator clicks "Clear Halt" (`POST /api/halt/clear`).
  7. When the operator attempts to stage or place a new trade, `_evaluate()` checks `self._pending_intents()`.
  8. Because the intent is `NOT_FOUND`, it remains in `PENDING_INTENT`. `chk("no_unknown")` fails: "an earlier order is UNKNOWN or unreconciled".
  9. `resolve_intent()` exists on `Guard` (`px_guard.py:670`), but there is no HTTP route in `px_app.py` and no button in the UI to call it.
  10. Result: The desk is permanently bricked for all future opens until the user manually deletes `data/intents.json` from the command line.
- Fix:
  1. Add a route `POST /api/intent/resolve` in `px_app.py` taking a tag and note, and expose a "Resolve Unconfirmed Order" button in the desk UI.
  2. Alternatively, let `reconcile()` allow an explicit user confirmation parameter to transition `NOT_FOUND` to `RESOLVED`.
  Missing test: In `tests/test_px_app_actions.py`, trigger an unknown intent, call `reconcile()`, clear halt, and assert that a subsequent `/api/preview` call succeeds after resolving the intent.

### F3 Partial fills trigger false consecutive loss lockouts  [severity: high]  [confidence: verified]
- Where: `px_state.py:202-222`, `px_guard.py:326-330`.
- Failure sequence:
  1. Trader holds 2 contracts (within `PX_MAX_QTY_SYMBOL=2`).
  2. The stop loss is hit on the broker.
  3. The broker matching engine fills the 2 contracts against two distinct bids, emitting two separate `GatewayUserTrade` fill events, each with negative `profitAndLoss`.
  4. `LiveState.closed_trades()` appends both fills as individual entries in the closed trades list (`px_state.py:220`).
  5. `Guard.behavior()` counts consecutive losses by iterating `reversed(closed)` (`px_guard.py:326-330`).
  6. `consec` increments to 2 from a single losing trade.
  7. If the next trade also closes in two partial fills, `consec` becomes 4, exceeding `PX_MAX_CONSEC_LOSSES=3`.
  8. Result: The trader is locked out of the entire session due to fill execution fragmentation rather than consecutive losing trades.
- Fix:
  Aggregate closed fills by contract and order ID or group fills occurring within a 5-second window before evaluating consecutive losses.
  Missing test: In `tests/test_px_state.py`, append two trade fills with the same timestamp or order reference, and assert that `Guard.behavior()["consecutive_losses"]` equals 1, not 2.

### F4 Position inversion race in flatten fallback  [severity: high]  [confidence: verified]
- Where: `px_guard.py:828-851`.
- Failure sequence:
  1. The operator or watchdog triggers `flatten()`.
  2. `flatten()` sends `POSITION_CLOSE` (`/api/Position/closeContract`).
  3. It calls `_verify_flat()` which polls `_positions_rest()` 10 times at 1.0s intervals.
  4. Due to broker matching engine latency or REST read replica lag during high volatility, the position still shows as open after 10 seconds.
  5. `flatten()` reaches line 843 and submits an opposite market order (`ORDER_PLACE`) sized to the net position.
  6. The initial `closeContract` execution finally settles on the broker exchange right before or concurrently with the opposite market order.
  7. Both orders execute.
  8. Result: The trader's position flips from Long 1 to Short 1 with no attached stop loss.
- Fix:
  1. Tag the opposite market order with a specific cancel-replace or OCO dependency if supported by the broker.
  2. If `_verify_flat` fails after `closeContract`, trip the halt immediately without firing an opposite market order, or require operator confirmation before sending the secondary market order.
  Missing test: In `tests/test_px_guard_flatten.py`, simulate `closeContract` executing after 11 seconds while an opposite market order is in flight, and assert that net exposure does not invert.

### F5 Open position risk is omitted from remaining budget calculation  [severity: medium]  [confidence: verified]
- Where: `px_guard.py:274-282, 440-456`.
- Failure sequence:
  1. Account has `$1,000` daily loss limit and `$2,000` drawdown buffer.
  2. Trader opens 1 MES risking `$240` on a resting stop.
  3. While Trade 1 is open, trader stages an order for 1 MNQ (`PX_MAX_QTY_TOTAL=3` allows this).
  4. `budget()` calculates `remaining = min(daily_loss_limit + pnl, dd_buffer)` (`px_guard.py:281`).
  5. Because Trade 1 is still open, `pnl` is `$0.00` (realized only).
  6. `remaining` evaluates to `$1,000.00`. It does not subtract the `$240.00` worst-case risk currently committed to Trade 1.
  7. Trade 2 is sized against the full `$1,000.00` remaining budget, allocating another `$240.00`.
  8. If both trades stop out, the actual loss is `$480.00`, violating the intended single-trade risk constraint relative to the original budget.
- Fix:
  In `budget()`, calculate `open_risk = sum(t.readback["risk_usd"] for t in open_trades)` and subtract `open_risk` from `remaining`.
  Missing test: In `tests/test_px_guard_preview.py`, stage a trade while an open position with an active stop exists, and assert that the computed budget is reduced by the active trade's stop risk.

### F6 Hostile stage superseding by unauthenticated local processes  [severity: low]  [confidence: verified]
- Where: `px_guard.py:480-482`, `px_app.py:285-304`.
- Failure sequence:
  1. The operator stages an order on the desk and prepares to hold the PLACE button.
  2. Any local script or browser process on the host hits `POST /api/preview` with an inverted or bogus trade.
  3. In `px_guard.py:480`, `_stage()` marks all existing `STAGED` tickets as `SUPERSEDED`.
  4. When the operator completes the 600 ms hold on the UI, the submission fails with `ticket: already SUPERSEDED`.
  5. While this does not place unauthorized money, it allows unauthenticated local processes to invalidate human orders at critical market entry moments.
- Fix:
  Require the session CSRF token or an origin identifier on `/api/preview`, and prevent MCP or non-UI origins from superseding a UI-staged ticket without an explicit `override=true` flag.
  Missing test: In `tests/test_px_guard_preview.py`, verify that a ticket staged with `origin="ui"` cannot be superseded by a ticket staged with `origin="mcp"` within its TTL.

## Answers to the 8 questions
1. **Order safety.**
An order can happen without a stop when a fill occurs during a SignalR hub disconnection. `_arm_stop` is only called from `handle_position`, which only listens to live SignalR events. The REST reseed snapshot (`_snapshot`) updates `positions` in `LiveState` but never emits events to `handle_position`. Furthermore, the 5-second watchdog is only spawned inside `_arm_stop`, so no watchdog timer runs. The position stays open and completely unstopped until manually closed.

2. **UNKNOWN handling.**
"Never replay, halt, reconcile by tag" is theoretically sound, but dangerously dependent on unverified gateway mechanics. If TopstepX `/api/Order/search` does not return `customTag`, every reconcile query yields `NOT_FOUND`. Because `NOT_FOUND` is classified as a pending intent, the desk permanently refuses all future orders with no UI route to clear it.
In a sim rehearsal, verify in this exact order:
(1) Place a 1-lot order with `customTag`, fetch `/api/Order/search` across that timestamp, and verify `customTag` is returned unaltered.
(2) Test maximum tag length accepted by the broker.
(3) Drop the client connection immediately after sending POST data, check if the broker fills the order, and confirm that `search_orders` retrieves it.
(4) Verify `PositionType` integer mapping for Short positions.

3. **Prop-firm rules.**
The "conservative budget" model is not defensible for evaluation accounts. Real prop firms (Topstep, Apex) calculate trailing drawdown from the intraday unrealized high-water mark, not realized trade exits. A trade that goes up $800 and retraces to break-even reduces drawdown room by $800 on Apex tick-by-tick or at day close on Topstep. The desk's static `PX_DRAWDOWN_BUFFER` is completely blind to this. Additionally, prop firms mandate hard flat-by times (e.g. 15:10 CT for Topstep funded accounts). The desk blocks new entries after `PX_NO_NEW_AFTER` but never auto-flattens resting positions.

4. **AI surface.**
The allowlist sanitization is robust against prompt injection: contract descriptions are discarded, contract symbols must match strict alphanumeric patterns, external order tags are masked to `EXTERNAL`, and broker errors are mapped to static enum codes.
However, an agent can still cause operational disruption:
(1) Staging spam: calling `preview_order` in a loop continuously supersedes human tickets.
(2) Halt abuse: calling the `halt` MCP tool halts the desk, and because there is no MCP tool to clear a halt, human intervention is required to unlock it.
(3) Info leakage: `/ai/state` and `/ai/digest` on loopback require no authentication and leak exact position prices, quantities, and P&L if `stream=1` is omitted.

5. **Sizing and the behavioral rails.**
- Round-down sizing: Essential. The bedrock of risk control.
- Cooldown after loss (300s): Most valuable existing rail. Prevents immediate revenge trading.
- Lockout after 3 losses: High value, but broken by partial fills (F3).
- Reduce-only ADX/ATR scalers: Moderate value. ADX < 20 cut is noisy because breakouts often start from chop; ATR 80th percentile cut is solid.
- Size lock after loss: Good rule, prevents Martingale doubling.
- Single most valuable missing rail: **Intraday trailing drawdown floor tracker**. A rail that ratchets down allowable risk as unrealized profit retraces, preventing traders from giving back big winning days and breaching firm rules.

6. **Network exposure.**
The weakest link is HTTP Basic Auth over cleartext HTTP on `0.0.0.0`. Anyone sniffing LAN traffic or an unencrypted Docker bridge captures the 32-character `PX_OPERATOR_TOKEN`. Furthermore, `/mcp` routes bypass the operator login entirely and rely solely on `PX_MCP_TOKEN`.
Before container or LXC deployment:
(1) Bind strictly to `127.0.0.1` inside the host and front with Tailscale Serve or Caddy with mandatory HTTPS.
(2) Terminate plaintext Basic Auth in favor of a session cookie or header exchange.
(3) Add IP rate limiting on authentication attempts.

7. **Docs.**
Cold-read finding: The README claims the desk "sizes every trade from your remaining daily-loss and drawdown room" (line 11). This claim is unearned. The desk does not read firm drawdown rules, does not track high-water marks, and does not subtract open risk from running trades. A beginner will assume the software protects their eval drawdown automatically.
Missing: Clear instructions on where to obtain `PROJECTX_API_KEY` in TopstepX, and a prominent warning that position bracket mode places unbracketed market orders that rely on SignalR fill events to submit stops.

8. **Product.**
To make an eval trader keep using this:
(1) Direct integration with actual prop firm rules (auto-tracking trailing drawdown and hard auto-flatten at 15:10 CT).
(2) Audio alerts on fills, stops, and cooldown countdowns.
What to cut:
(1) Cut `gex_futures.py` and GEX panels from the default UI. It fetches nothing, displays `UNAVAILABLE` by default, and distracts from core execution discipline.
(2) Cut the limit order price-drift logic. Eval futures traders overwhelmingly use market orders with protective brackets.

## Ideas, ranked
| # | Idea | Why | Cost (S/M/L) | Risk it adds |
|---|---|---|---|---|
| 1 | Reseed position sweep for stop arming | Closes critical vulnerability F1 where a fill during hub disconnect leaves positions naked. | S | Must verify partial fill state does not duplicate stop orders. |
| 2 | HTTP route and UI button for `resolve_intent` | Resolves deadlock F2 where unconfirmed orders permanently lock out the desk. | S | Operator could resolve an order that later executes; must require explicit confirmation. |
| 3 | Aggregate partial fills in consecutive loss rail | Prevents F3 where a single 2-lot loss broken into two fills triggers premature lockout. | S | Requires buffering trades across a 5-second session window. |
| 4 | Deduct active open trade risk from remaining budget | Prevents F5 where multi-contract scaling over-allocates risk beyond the loss limit. | S | Makes sizing more conservative when scaling into multiple micros. |
| 5 | Hard session close auto-flatten daemon | Prevents prop firm rule disqualification for holding past the daily closing bell. | M | Market order slippage at illiquid closing seconds. |
| 6 | Intraday peak equity tracking for trailing drawdown | Aligns the desk's risk budget with real prop firm trailing drawdown rules. | M | Bad tick spikes on quote feed could falsely ratchet the trailing floor. |
| 7 | Native atomic OCO on simulated accounts | Eliminates separate stop orders and naked watchdog windows entirely. | L | Dependent on whether TopstepX supports OCO on simulated accounts. |

## What I did not check
1. I did not test against a live TopstepX / ProjectX gateway; all behavioral claims are validated against `dev_gateway.py` and static AST analysis.
2. I did not verify whether TopstepX `/api/Order/search` supports RFC 3339 timestamps with fractional seconds or expects UTC ending in `Z`.
3. I did not execute front-end browser automation tests against KLineChart rendering or verify DOM slot replacement in all browsers.
4. I did not verify the performance of `px_levels.py` placebo rate calculation under large bar histories exceeding 10,000 bars.
