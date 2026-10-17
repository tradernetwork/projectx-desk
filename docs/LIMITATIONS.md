# Known limitations and what is unverified

Read this before you trust the desk with anything. It is the honest list, kept next to the code so it does not drift.

## What has been verified against the real gateway, and what has not

**Verified read-only, on the real TopstepX gateway (Combine sim account, 2026-10-15 and 2026-10-16):** login and account lookup, contract
search, the market hub (quotes and the trade tape, about 85 ms behind the clock), the user hub connecting and seeding, REST bars
(`live: false` bars equal a candle rebuilt from the tape, OHLC and volume), and that `GatewayTrade.type` carries the aggressor side.
**Not verified: any order write.** No order has been placed, modified, cancelled or flattened on the real gateway.

Everything else is tested against the bundled mock gateway (`projectx_desk/dev_gateway.py`). The mock follows the ProjectX documentation,
but it is not the real thing. Not yet confirmed on a real account:

- the whole order path (place, stop after fill, flatten, cancel, modify)
- `PositionType` Short = 2
- the `live` data-subscription flag on bars and contract search (partly checked: `live: true` bars are refused for the Combine account, see below)
- how your account's bracket mode is detected (it is never guessed: `PX_BRACKET_MODE` must be set explicitly)
- whether order search returns `customTag`, which reconcile-by-tag depends on
- `Position/closeContract` semantics, especially on live accounts
- whether your eval or funded account reports `simulated: true`. That decides whether cancel and stop-modify (documented
  as simulated-only) are available to you.
- the SignalR hubs on a real account: authentication, the exact event payloads, and what arrives when. The whole live feed,
  including the fill event that triggers the stop, rests on them.
- the gateway's rate limits (the client stays under the documented 50 per 30 s for bars and 200 per 60 s otherwise)

## Known limitations

**On loopback (the default) the HTTP API has no operator login.** `GET /api/csrf` is open and the `Origin` header can be set by any
non-browser client. Host, Origin and CSRF checks defeat web pages and DNS rebinding; they do not stop a local process. Anything
that can reach `127.0.0.1:<port>`, including an AI agent with shell or browser-automation access, can stage and place within the
caps. The MCP server cannot place, but an agent that also has a shell is not bounded by it. Run agents without access to the
desk's port, or accept that your caps are the real limit.

**Any other bind needs the operator token, and the token is the whole gate.** `PX_HOST` other than loopback refuses to start
without `PX_OPERATOR_TOKEN` (32+ characters) and `PX_ALLOWED_HOSTS`. The token is a login (HTTP Basic) for every route except
`/mcp/` and `/healthz`, and whoever has it can place orders within your caps. HTTP Basic is cleartext, so TLS must sit in front of
anything beyond a network you fully control (see [DEPLOY.md](DEPLOY.md)). There is no rate limit on login attempts beyond the
token's length. If you front a loopback-bound desk with a reverse proxy, set the token too: the refusal only triggers on the bind.

**The floor is yours to maintain, and the open P&L is the desk's mark.** With `PX_MLL_FLOOR` set, drawdown room is the live balance minus
the floor, so it follows the account, but the desk does not compute Topstep's end-of-day trailing floor, its lock, your high-water mark or a
consistency rule: you update the floor. Without it, `PX_DRAWDOWN_BUFFER` is a number you type. Since Round 10 the room is also net of the
unrealised loss on every open position, marked at the last trade of a quote no older than `PX_MAX_HUB_AGE_S` (or of the stop risk our own
tickets commit, whichever is worse); a position the desk cannot value makes the room UNKNOWN and opens refuse (`open_pnl_known`). That mark
is not the firm's: it assumes the gateway's `balance` excludes open P&L (true of the mock, unverified on a real account) and it lags a fast
market by a quote. Treat the budget as an upper bound that you keep updated by hand.

**A stop is sent after the first fill event, not with the entry** (in `position` bracket mode). If the user hub is down at
fill time, no stop is sent and no watchdog runs. In `oco` mode the bracket is atomic. The ticket says
"UNPROTECTED UNTIL STOP ACKED" while the stop is pending.

**Ambiguity is handled per write, not uniformly.** An ambiguous entry becomes `UNKNOWN`, writes a persistent halt file, and
blocks new opens until a human reconciles. An ambiguous or failed stop flattens and halts. `FLATTEN` halts and tells you to close in
the TopstepX UI if the close cannot be confirmed. Only with `PX_FLATTEN_FALLBACK=1` (default 0) does it send a second, different
closing order (an opposite-side market order capped at the net position read just before sending), and a late-executing first
close followed by that order could flip a position. Writes are never retried or re-sent byte for byte.

**Reconcile is manual.** `POST /api/reconcile` searches for the order by `customTag`; it never runs automatically and nothing
auto-clears a halt. Deleting `data/` clears the halt, the day's order count and the persisted intents.

**Live accounts are blocked by policy and by flags, not by a hard code wall.** A live account needs `PX_ACCOUNT_KIND=live`,
`PX_ALLOW_LIVE=1`, `PX_BRACKET_MODE=oco`, and market entries. It has never been run. Do not enable it.

**The browser cannot tell an AI extension's click from yours.** The 600 ms hold is anti-fat-finger friction. The server-side
caps are the control.

**Stream mode is client-side removal plus server redaction on the AI endpoints.** `/api/preview`, which only the UI calls,
returns dollar figures and the last digits of the account id regardless of stream mode, and "Reveal 30s" brings them back.
Prices, sizes and stop levels stay visible by design.

**Gateway text.** Contract names are reduced to an uppercase code or derived from the contract id; the gateway's description
is never forwarded; order tags are shown only as OWN or EXTERNAL; gateway error text is never repeated. Treat anything an
agent reads from this desk as data, not instructions.

**The AST pin is a tripwire, not a proof.** It catches the usual ways to name an order endpoint or call the write method
from another module, and `PXClient.post` independently refuses any path that is not a known read endpoint. Indirect or
dynamic construction can still evade a static check, so review new network code by hand.

**Platform.** The test suite passes on Python 3.10, 3.11 and 3.13 on Linux. macOS should work and is untested. On Windows use
WSL: `run_dev.sh` is a bash script that needs `curl`. `tzdata` is a requirement because the session clock uses
America/Chicago.

**`PX_MCP_PLACE` is a reserved flag.** No route sets an MCP origin on `place()`, so it does nothing today.

**GEX is display only, and needs a key.** `projectx_desk/gex.py` fetches levels from the TraderDaddy API (`PX_GEX_KEY`) and converts them with
`projectx_desk/gex_futures.py`; without a key the digest says `GEX: status=NO_KEY` and nothing is drawn. The levels are a translation of index and
ETF option positioning, not a measurement of the futures book, and they never feed sizing, stops or targets (Round 6).

**Thresholds are hypotheses.** The ADX and ATR scalers (off by default since Round 10: the ATR cut prices volatility twice), the
behavioral-rail defaults and the level-reaction verdict margin are starting points, not findings. The cost line's slippage default
(`PX_SLIPPAGE_TICKS=2`) is an assumption too; your fills decide the real number.

**The behavioral rails count closed orders, not round trips.** Cooldown, lockout and the size lock read today's fills that
carry a realised P&L, grouped by order id (`closed_trades()`): one order filled in pieces counts once. Two separate closing orders
for one position count as two, and a scratch (zero P&L) neither extends nor breaks a streak. Setting `PX_COOLDOWN_AFTER_LOSS_S` or `PX_MAX_CONSEC_LOSSES` to `0` turns that
rail off on purpose; a blank, garbled or negative value falls back to the default. `PX_SIZE_AFTER_LOSS_LOCK=0` switches that rail off;
`PX_SIZE_SCALERS=1` switches the ADX/ATR cuts on.

**Quote age and drift.** The 2 second quote age applies to every open. The price-drift check (8 ticks since staging) applies to
market entries only; limit opens (simulated accounts only) skip it.

**A 401 or 429 on an order write becomes UNKNOWN and halts**, even though the order was certainly not processed. That is the
safe direction, and it means an expired token will halt the desk until a human clears it.

**Level reactions are a measurement with thresholds, not a finding.** "Held" means price moved at least 0.75 ATR away within 6
bars before closing 0.25 ATR through the level. The comparison is the same test on copies of that line shifted 0.5 to 1.5 ATR
away, which are not independent samples. Since Round 10 a positive verdict needs 30 resolved tests, a 15 point gap over the calibrated
placebo, a one-sided binomial p-value, and that p-value surviving a Benjamini-Hochberg cut (q = 0.10) over every candidate on every
timeframe in the scan. The placebo is calibrated on three block-bootstrap surrogates, not a 1000-draw null distribution: the p-value is
exact against the calibrated rate, not against the surrogates. The old `LEVEL:` lines still use the raw placebo and the effect-size
gate alone.

**Stream mode is covered by tests on the data endpoints and the page's data, not on the rendered DOM.** The removal is real in
the code (`applySlots` swaps the figures out of the page) but no browser test pins it.

**The digest and the desk are open to anything on loopback.** The MCP server has a bearer token; `/ai/*`, `/state.json` and the
POST routes do not, for the reason given above.

## Known gaps

- The existing `LEVEL:` lines (EMA 8/21/34, VWAP on the current timeframe) still use the raw `projectx_desk.levels` placebo, which is biased
  for tight or self-referential levels (EMA 8 reads about 19 points low on random walks). `projectx_desk/respect.py` recalibrates; the old lines
  do not yet. Level-reaction events are also capped at 12 and pending events in the last 6 bars are never counted.
- The respect scan is descriptive history on 25 hand-picked candidate levels. It is not backtested for trade outcomes, and a
  level that was respected is not evidence that it will be. The candidate set mixes lines with a mechanism (prior-day high/low, VWAP:
  resting orders and benchmark flow sit there) with lines that have none (an EMA 34 on a 5m chart); the scan treats them alike.
- The analyst callout feed is an unmeasured signal. The one-click STAGE on a fresh ENTRY is off unless `PX_FEED_STAGE=1`, and when
  used it is audited (`stage_source=callout`), but no backtest of the callouts exists and the desk stores no history to build one from.

- `PX_NO_NEW_AFTER` only blocks new entries. Set `PX_FLAT_BY` to get the auto-flatten (Topstep requires flat by 3:10 PM CT); it is off until you set it, and it does not know holiday or early-close schedules.
- Drawdown room is the live balance minus `PX_MLL_FLOOR` if you set it, otherwise the number you type. The desk does not compute Topstep's end-of-day trailing floor or its lock; you update the floor. Unrealised loss counts through the desk's own mark of each open position (Round 10), which lags the firm's by a quote. The daily loss limit is your own number, not read from Topstep.
- Topstep objectives (profit target, 55% and 40% consistency, payout rules, the scaling plan) and conduct rules (news, hedging across accounts, prohibited strategies) are not enforced. See [TOPSTEP_RULES.md](TOPSTEP_RULES.md).
- `last_risk` (the size-after-loss lock) is replaced at commit time by a subsequent order even if the broker then rejects it.
- External trades (placed outside the desk) have no known risk cap.
- **Holiday and early closes need your input.** The desk ships no calendar. Topstep's help article on early closes says to be flat a fixed
  number of minutes before the close (their 2024 and 2025 articles said 30, a later one 15), so the lead time has changed. Set
  `PX_EARLY_CLOSES="2026-11-27=12:00,2026-12-24=12:00"` (CT, `YYYY-MM-DD=HH:MM`, copied from Topstep's calendar) and on those dates the desk
  flattens at that time and refuses opens from two minutes before it. Garbled entries are ignored, never guessed.
- A missing day-state file resets the order count.
- `/state.json` and `/contracts` still forward some raw gateway fields (quote timestamps, contract id fallback names) that should be type-validated.
- A position held at restart that was placed by hand is reported (`EXTERNAL_UNPROTECTED`) but not protected.
- Scale-in is not supported: a second entry on a contract with a position, working order or entry in flight is refused.

- **`live: true` on bars is refused for the Combine account** (verified 2026-10-16: `success:false, errorCode:1`; `live: false` returns bars that match the market-hub quote to the tick). `PX_LIVE_DATA=1` therefore blanks ATR, VWAP and sizing (unreadable inputs size to 0). One `live_data()` in `projectx_desk/client.py` now feeds the chart, digest, respect scan and MCP; before this the digest and MCP hardcoded `False`. Likely needs a live market-data entitlement on the account; unconfirmed.
