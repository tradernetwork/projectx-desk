# AGENTS.md: ProjectX Desk

Guidance for AI coding agents (Codex, Claude Code, others) and for humans. Read `docs/DECISIONS.md` before changing
the guard: every rule below has a reason there.

## Run and test

```
# Windows PowerShell: py -m venv .venv; .venv\Scripts\Activate.ps1
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                      # offline: runs against the mock gateway; no broker, no credentials. Python 3.10+
./run_dev.sh                   # offline desk + mock gateway: http://127.0.0.1:8613/?ai=1
powershell -ExecutionPolicy Bypass -File run_dev.ps1   # the same on native Windows
python scripts/screenshots.py  # regenerates docs/img from the OFFLINE mock desk (needs requirements-screenshots.txt + playwright install chromium)
```

`python -m projectx_desk` loads `.env.projectx` first (shell variables win; only key names are printed).
All code is the `projectx_desk/` package: one module per job (names below are relative to it), the UI in `projectx_desk/static/`.
Start it with `python -m projectx_desk` from the checkout root. `data/` (runtime state, gitignored) and `.env.projectx` live at the
checkout root, never inside the package; `tests/conftest.py` puts the root on `sys.path`. Keep new modules inside the package.

## Layout

| File | Job |
|---|---|
| `projectx_desk/guard.py` | THE ORDER PATH: preview (checks, sizing, hashed ticket), place, stop-after-fill, watchdog, flatten, cancel, move_stop, reconcile, halt, audit |
| `projectx_desk/client.py` | REST. Reads retry once on 401; `write()` makes ONE attempt (guard only) |
| `projectx_desk/hub.py` | SignalR client for the user and market hubs; replays subscriptions; signals resync |
| `projectx_desk/state.py` | live quotes/positions/orders/trades; trading-day P&L; allowlisted `position_view`/`order_view` |
| `projectx_desk/limits.py` | config (every limit fails closed) and persisted day state |
| `projectx_desk/digest.py` | the AI surface: one state dict, `/ai/state` JSON and `/ai/digest` text |
| `projectx_desk/mcp.py` | FastMCP tools (read, size, stage, halt). No place/flatten/cancel/clear |
| `projectx_desk/net.py` | bind and exposure rules: loopback detection, host/origin allowlists, the operator login, the refusal to start unsafe |
| `projectx_desk/app.py` | FastAPI: chart data, SSE ticks, CSRF-gated POST routes, mounts MCP |
| `projectx_desk/levels.py` | EMA stack, ATR series, level-reaction statistics with a random-line baseline |
| `projectx_desk/indicators.py` | which indicators the chart shows for the timeframe on screen, decided server side: EMA 21, VWAP and volume are always on; EMA/SMA/BOLL/Keltner/fib/prior levels only when RESPECTED through the three `respect.py` gates AND near price (fib within 1.5 ATR, others 3); MACD and RSI only when ACTIVE (a regime gate, not an edge claim). Pure, no I/O. Served at `/api/indicators`. `PX_TRACK_ROOTS` (default `MES,MNQ,MGC`, `0` off, max 4) pre-subscribes those tapes at boot and re-scans every `PX_TRACK_INTERVAL_S` (300, min 60) |
| `projectx_desk/grades.py` | append-only `data/indicator_grades.jsonl` (`PX_GRADES_FILE`): one line per (contract, tf, closed bar) with the scored candidates and what was shown, for later grading. Rotates at 50 MB. Never breaks the loop |
| `projectx_desk/respect.py` | which levels each timeframe (5m/15m/1h/4h/1d) respects: ~25 candidate levels scored against a placebo calibrated on block-bootstrap surrogates, with a no-lookahead guarantee and a half/half stability check. RESPECTED needs 30 tests, a 15-point gap, a binomial p-value AND a Benjamini-Hochberg pass over the whole scan (Round 10). Pure, no I/O |
| `projectx_desk/flow.py` | real buy/sell volume from the trade tape (`GatewayTrade.type`: 0 buy, 1 sell), 1-minute buckets per contract. In-memory logic is pure; I/O is only the SQLite file. No tick history exists, so coverage starts when the desk subscribed (`since`); minutes touching a reconnect gap or a bad trade are `partial` and never drawn. Unknown side is counted apart, never guessed. Persisted to SQLite `data/flow.db` (`PX_FLOW_DB`): every minute, hole and partial flag, no cap (`PX_FLOW_KEEP_DAYS` prunes, default 90, 0 = forever); the newest `MAX_MINUTES` per contract also live in memory. An old `flow.json` (`PX_FLOW_FILE`) is imported once and renamed `.migrated`. A restart is always a hole. `/api/flow?after=<ms>` returns only the tail, which is what the UI polls. Served at `/api/flow`, drawn as the BUY/SELL VOL pane, summarised in the digest as `FLOW:` |
| `projectx_desk/anchor.py` | pure level-anchored stop and target (respected levels only, never GEX), plus `projection` (room left if the stop fills) and `heat`. Fails closed. See Round 6 |
| `projectx_desk/gex.py` | GEX levels from the TraderDaddy API converted to futures price space: display and digest only, never sizing. `PX_GEX_KEY`, `PX_GEX_URL`, `PX_GEX_MAX_AGE_S` |
| `projectx_desk/ticks.py` | tick bars (N trade prints, `PX_TICK_BARS`) built from the live tape with buy/sell volume; no history, in memory |
| `projectx_desk/feed.py` | analyst futures callouts from TraderDiscord-v2 `GET /analyst-callouts` (`PX_FEED_URL`, `PX_FEED_KEY`, `PX_FEED_CHANNELS`, `PX_FEED_HOURS`; unset URL = off). Third-party text is untrusted: only allowlisted names/enums/numbers leave the module, never message text. Opinion, not signal; no path to the guard. State key and digest lines are `callouts`/`CALLOUTS` (`feed` is the market-data health block) |
| `projectx_desk/recorder.py` | the recorder's history import for a trading desk: `PX_RECORDER_URL` (https or loopback http) + `PX_RECORDER_TOKEN`; untrusted JSON reduced to finite numbers, fixed error codes; pure merge (local complete minute wins, recorder fills holes, nothing summed or interpolated). See Round 15 |
| `projectx_desk/push.py`, `projectx_desk/notify.py` | Web Push for phone alerts. `push.py`: VAPID (ES256) and RFC 8291 encryption on `cryptography` + `httpx` (no new dependency), subscription store (`PX_PUSH_FILE`) and key file (`PX_VAPID_FILE`), https-only endpoints on a push-service allowlist (SSRF), 404/410 prune. `notify.py`: read-only background task that diffs the `/ai/state` dict and a few read-only closures and sends fixed templates; no path to the guard. UI in `static/alerts.js`, worker in `static/sw.js` (push and click only, never caches or intercepts fetch). See Round 16 |
| `projectx_desk/lock.py` | exclusive process lock on `data/desk.lock` (`PX_LOCK_FILE`), taken in `main()` only: a second desk on the same checkout refuses to start. Kernel-released, so a crash leaves no stale lock |
| `projectx_desk/tradeday.py` | the one definition of the trading day (17:00 CT roll) |
| `projectx_desk/sizing.py`, `projectx_desk/gex_futures.py` | pure sizing math; GEX levels converted to futures price space |
| `projectx_desk/dev_gateway.py` | a mock ProjectX gateway (REST + SignalR) for offline development and end-to-end tests |
| `projectx_desk/regime.py`, `static/regime.js` | chop and late-in-the-move measures (ADX per timeframe, 12-bar efficiency ratio, VWAP crossings, run off the 24-bar extreme in ATRs). Pure, display and logging only, never read by the guard or sizing. The JS twin draws the chip on the 15m and 1h minis; `tests/test_regime.py` runs both on the same bars. `regime.ema_slope` (UP/DOWN/FLAT, 5 bars vs ATR, `slope21` in the state and digest `EMA:` line, `s21` in the bias log) and its JS twin `emaSlope` share one arithmetic (EMA seeded with the first close, simple-mean ATR14; do not swap in `levels.ema_series` or `sizing.atr`); `regime.js` also holds `hma` for the opt-in HMA 21 chart pin (display only, never in the respect scan). `_bias_snapshot` logs the numbers (`er5`, `vwapx`, `run_up`, `run_dn`, `adx15`, `er15`, `adx1h`, `er1h`) |
| `projectx_desk/tomorrow.py`, `projectx_desk/tomorrow_feed.py` | Tomorrow's Map: the three roads (up, down, else) out of price from dealer gamma that survives tonight, with the IF / IF / ELSE plan. `tomorrow.py` is pure and a faithful port of the TraderMatrix builder (itself the mjs): `tests/test_tomorrow.py` pins exact sentences on three captured SPY books in `tests/fixtures/tomorrow`, so do not change a threshold or a sentence without bumping `VERSION` and the fixtures. Structure is computed in source space (SPY); `ratio` and `tick` convert only the OUTPUT, so every number in a sentence is regenerated from converted prices. `tomorrow_feed.py` fetches `/gex/{sym}`, `/matrix` and `/historical?hours=168` with the GEX key, takes the ratio from the futures 5m close at the snapshot time (never the live last), refuses on a wild ratio or no reference, and serves read-only `GET /api/tomorrow?contract=`. Display only, never read by the guard or sizing. The chart draws it as overlays past the last bar, the panel is `#tmap` |
| `projectx_desk/tomorrow_grade.py`, `projectx_desk/tomorrow_ledger.py` | the map's record. `tomorrow_grade.py` is pure and a port of the TraderMatrix grader, pinned by `tests/test_tomorrow_grade.py` to 450 map/session pairs whose reference output was produced by running the TypeScript itself (`tests/fixtures/tomorrow/grade_reference.json`); levels are scored on the CLOSE, a level never near is untested, the put wall is reported not scored, every level has a round-number baseline, and no rate is shown under 10 graded sessions. `tomorrow_ledger.py` files each tracked contract's map after the cash close (15:10 CT, weekdays) into `data/tomorrow_ledger.jsonl` (`PX_TOMORROW_LEDGER`) and grades it against the next 08:30 to 15:00 CT session bar; read-only, display and logging only. `/ai/state` carries `tomorrow` (cache or background load, never blocks) and the digest prints `MAP:` lines |
| `projectx_desk/mapplan.py`, `projectx_desk/mapplan_log.py` | SHADOW-MODE proposals from dealer-map events (Round 21, stage 1 of 3). At each 5m close the desk detects an event on the CLOSED bar (trapdoor/shelf/cushion lost, accelerant/brake/ceiling cleared, flip/brake/ceiling rejected, trapdoor/shelf reclaimed), works out the plan it WOULD have made (entry = the bar close, stop = 1.5 x ATR, never a map or GEX level; target = the next map level beyond 1R, advisory only) and logs it to `data/map_proposals.jsonl` (`PX_MAPPLAN_FILE`), then grades it against what price did (a bar touching both stop and target counts as the stop; the OPPOSITE side with the same stop distance is the baseline; flat-by 15:08 CT ends a trade). Cooldown 30 min per (kind, level), 8 per contract per day, a map only on the day it was made, stop bounded to the guard's 4 to 200 ticks. `scripts/mapplan_report.py` prints the record; no rate under 30 graded per kind. Nothing is shown, nothing is stageable, and a test pins that neither module imports the guard, client or hubs or calls place/preview/write. Stage 2 (display) and stage 3 (a stage button behind a default-OFF switch) need the record to beat its baseline first |
| `projectx_desk/biasbt.py` | offline scorer for the BIAS rule from `data/bias_log.jsonl` (outcomes, coin-flip baseline, walk-forward folds, Monte Carlo streaks, audit). Pure; run it with `scripts/bias_backtest.py`. Never imported by the desk |
| `projectx_desk/static/` | the desk UI (vanilla JS, no build step) |
| `projectx_desk/static/trend.js` | automatic trendlines and channels from the candles on screen. Pure and node-tested (`tests/test_trend.py`); display only. Swing pivots (4 bars each side); a support or resistance line needs 3 pivot touches within 0.3 ATR, at most one close through it by 0.25 ATR, a recent last touch, and not already broken by more than an ATR; a channel is a line plus a parallel rail that 2 pivots also touch; a least-squares regression channel appears only when no pivot line fits and R^2 is at least 0.5. Every drawn line is labelled with its touch count. Lines found on the other timeframes (1m, 5m, 15m) are drawn on whichever chart is on screen, converted through TIME (`timeLines`, `indexAt`), dashed and tagged with their timeframe, with one toggle per timeframe. Touches are counted, NOT tested against a random-line baseline the way `respect.py` tests levels: a line says where price turned, not that it will again. Overlay groups `tmap` and `trend` are in `NONUSER` so they are never saved as the trader's own drawings |
| `projectx_desk/static/tmap.js` | the dealer map's words and level logic, pure and node-tested (`tests/test_tmap.py`): role glosses, watch lines, merged strikes (`tmapLevels`), the right-now line (`nowText`) and `roleColor`, which keeps a TRAPDOOR or LAST SHELF out of the road's "go" green. `desk.js` only draws with them. The GEX bands skip the OHLC legend corner (`LEGEND_W`/`LEGEND_H`) so the text stays readable |
| `projectx_desk/static/watch.js` | level-cross detection for the dealer map: pure, node-tested (`tests/test_watch.py`). A cross is price moving to the other side of a level by more than one tick; the first sight of a level is a silent baseline (loading the page never fires); the same level and direction waits 2 minutes. `desk.js` turns an event into a flashing banner, a chime and a flashing tab title that say what HAPPENED ("TRAPDOOR BROKE 7704.5", "an alert, not a signal"), never a call to act. Display only; a test pins that the alert path has no POST and no trade wording |
| `projectx_desk/static/layout.js` | Auto / Landscape / Portrait: sets `html[data-layout]` before first paint; the portrait grid lives at the end of `desk.css` (own footer row, full-width chart). Display only |

## Rules with teeth

- **Topstep rules live in `docs/TOPSTEP_RULES.md`** with sources and a per-rule status. `PX_FLAT_BY` (auto-flatten, refuse opens in the last 2 minutes) and `PX_MLL_FLOOR` (drawdown room = live balance minus the firm's floor; unreadable balance refuses) are enforced by the guard; do not weaken either. Orders must originate from the trader's own device (Topstep API terms): never document or ship a cloud-VPS deployment as the recommended path.
- **Only `projectx_desk/guard.py` may name an order-write endpoint or call `PXClient.write`.** Pinned by AST in
  `tests/test_px_guard_pin.py` (attribute access, getattr, string literals). Do not narrow the pin or add a second
  order path. MCP and everything else may only `preview` and `halt`.
- **Kill switches default OFF** (`PX_TRADING=0`, `PX_MCP_PLACE=0`). A zero or blank money or quantity limit refuses opens, as does
  a blank account id, `PX_NO_NEW_AFTER` or `PX_BRACKET_MODE` (never guess: a wrong guess still creates a rejected order). The two
  behavioral rails are the exception: `0` turns them off, blank or garbled falls back to the default.
- **One desk per checkout.** `lock.acquire()` runs in `app.main()` before uvicorn; an unopenable lock file refuses to start. Do not move it into
  `create_app()` (tests and tools build apps freely) and do not add a bypass flag. It does not cover two machines on one account: that is yours to avoid.
- **Never replay a write.** Any ambiguous outcome is UNKNOWN: persist, trip the halt file, reconcile by `customTag`.
  Only a human clears a halt (there is deliberately no MCP tool).
- **One protection owner per net position.** The `contract_flat` check refuses a second entry while the contract has a position,
  a working order or an entry in flight. No scale-in. Open stop risk of live tickets is subtracted from the budget used for sizing.
- **Every open position must be protected.** `Guard.protect_open_positions()` runs after boot and every resync: known ticket arms or
  resizes the stop, a broker stop that covers it is fine, our own persisted intent with no provable stop is flattened and halts,
  anything else is reported `EXTERNAL_UNPROTECTED` and never touched.
- **Flatten does not guess.** No automatic opposite-order fallback unless `PX_FLATTEN_FALLBACK=1` (default 0). A failed cancel is an
  orphan: `flat=False` and halt. A stop that ACKs after the watchdog flattened is cancelled (sim) or reported as an orphan (live), and halts.
- **A stop resize re-sends the CURRENT stop price** and takes the stop lock; never widen a stop the trader tightened.
- **`place()` re-checks halt, ticket expiry and quote freshness synchronously at the commit point**, after the awaited REST reads.
  A broker rejection that carries an `orderId` is still possible exposure: if a position appears it is protected and the desk halts.
- **`PXClient` read wrappers raise** on `success:false` or a non-list payload; an empty list means empty. MCP preview cannot supersede a
  ticket staged at the desk (`ui_ticket_pending`). `POST /api/intent/resolve` is human-only; give it no MCP tool.
- **The loss room counts open P&L.** `Guard.draw()` is the worse of the stop risk our tickets commit and the unrealised loss on every open
  position, marked from a fresh quote; both rooms are net of it. A position the desk cannot value makes the room UNKNOWN and opens refuse
  (`open_pnl_known`). Never let a gain widen the room, and never default an unvaluable position to zero.
- **A level is RESPECTED only through all three gates** in `respect.py`: effect size (`levels.MIN_TESTS`=30 tests, 15-point gap), the exact
  binomial p-value, and the FDR pass over the whole scan. Do not lower `MIN_N`, `HALF_MIN_N` or raise `FDR_Q` to make the panel look busier;
  `anchor.py` trusts that verdict with real stops.
- **`bias.py` is advisory trend context, NOT a signal** (rule v2.2; v2.1 first backtest `docs/DECISIONS.md` Round 20 found no edge, `scripts/bias_backtest.py` re-scores it from the log; `docs/DECISIONS.md` Rounds 17 and 18). Hand-set, never fitted: stacks, VWAP and tape votes plus a graded entry-location term (PULLBACK to the 21 EMA scores, EXTENDED scores nothing), graded penalties for ATR percentile, stretched RSI and ADX (no cliffs), and caps only for qualitative states (DISSENT and EMA_TURN, the 5m EMA21 slope curling against the lean, to B; EXTENDED, THROUGH_EMA and the first 15 minutes of the 08:30 CT open to C). Score is out of `MAX_SCORE` (3.5). Never read by `guard.py` or sizing. The chip shows only when `/ai/state` is for the chart's own contract. `_bias_log_loop` appends one line per tracked contract just after each 5m bar closes to `data/bias_log.jsonl` (`PX_BIAS_LOG_FILE`), keyed on the closed bar, with the rule version, for out-of-sample scoring.
- **Size is the ATR stop only.** ADX is an advisory regime (`regime` in the state, `REGIME:` in the digest: RANGING below 20), never a sizing input and never a guard check. **The ADX/ATR scalers default OFF** (`PX_SIZE_SCALERS=1` to enable): the ATR stop already prices volatility. The one-click STAGE on analyst
  callouts defaults OFF (`PX_FEED_STAGE=1`); when used, the ticket is audited as `stage_source=callout`. The cost line needs
  `PX_COST_RT_USD`; unknown cost prints NA, never 0.
- **Readiness is the same evaluation, read-only.** `Guard.readiness()` (POST `/api/readiness`, same Origin + JSON + CSRF gate) runs `_evaluate` and stops: no ticket, no audit line, no write. The manual stop box defaults to min(ATR stop, `PX_MAX_STOP_TICKS`). The desk's own ensure-quote waits use the guard's `max_quote_age_s` (2 s), so a quote the UI called fresh is never refused as stale. A halt shows its cause (`halt_cause`, a fixed word or `unknown`, never file free text), and a PLACED entry that is neither working nor filled after the 10 s grace shows `entry_status: gone`.
- **Manual BUY/SELL is the same order path.** `qty` on `/api/preview` (UI only, never MCP) makes the guard derive the risk from the trader's
  contracts and stop and run every check unchanged; `manual_qty` refuses with the number the room and caps allow, never sizes down silently.
  Do not add a check bypass for manual orders; the rails have their own `0` switches.
- **Sizing is always computed** and rounds down. 1R is `risk_pct` of the live drawdown room (balance minus `PX_MLL_FLOOR`), never of the account
  balance and never above the remaining daily or drawdown room; unreadable inputs size to 0 (`PX_RISK_BASE`, `docs/DECISIONS.md` Round 4). ADX/ATR scalers and the behavioral rails only ever refuse or reduce.
- **Stops only tighten** (`move_stop`): toward price, never wider, never into the market, simulated accounts only.
- **The AI surface is an allowlist** of numbers, enums and validated names. No account id, no balance, no gateway free
  text: a hostile contract name or order tag must never become a prompt.
- **Nothing in the browser can tell an AI extension's click from a human's.** The 600 ms hold is anti-fat-finger, not
  security. Real controls are server-side caps, the hashed single-use ticket, the fresh-state gate, qty cap 1 and
  sim-first.
- **`PX_RECORD_ONLY=1` is log-only by construction.** `create_app` returns before it registers any POST route, builds no Guard, user hub, account binding, watchdog or protection, and never mounts MCP; it refuses to start if `PX_TRADING` or `PX_MCP_PLACE` is set. Do not add an order, halt or flatten path to that mode, and do not make it optional per route: Topstep lets a server read and log, nothing else. Tick bars persist to `data/ticks.json` (`PX_TICKS_FILE`) and a restart is always a hole, never bridged.
- **Weekend quiet is for self-initiated traffic only.** `tradeday.market_quiet()` (Fri 16:05 CT to Sun 16:55 CT, holidays not modelled) plus a provably flat desk (`app._quiet`: positions seeded, none open, no working orders) makes the hub reconnect loops, the respect rescan, the bias-log fetch and boot retries stand down. Chart loads a human triggers still hit the API (candles are REST). Anything unknown or exposed is NOT quiet. `PX_WEEKEND_QUIET=0` disables it; blank or garbled keeps it on. `/state.json` shows `quiet`.
- **Phone alerts are read-only and say nothing sensitive.** The notifier never touches `guard.py`; text is fixed templates built from validated enums (never an account id, balance, order tag, contract free text or callout text); dollars only with `PX_PUSH_AMOUNTS=1`. The service worker must not cache the app shell or intercept fetch: a stale desk is dangerous. The four `/api/push/*` POSTs exist only in full desk mode and use the same Origin + JSON + CSRF gate; `PX_RECORD_ONLY` keeps zero POST routes. Subscription endpoints must stay https on the push-service allowlist.
- **GEX levels are converted with a frozen basis.** `gex._basis_ref` fixes the futures reference price once per payload; converting with the live last made basis = live last minus a stale spot, so the pin and walls rode along with price. Keep the freeze and its test.
- **Branding.** Every link to tradermatrix.pro carries `?ref=MPHINANCE` (the referral convention shared with TickerTrace and Vero: a link that loses its code converts and pays nobody). The README footer keeps the network line and the referral disclosure. No ads, no analytics SDKs, and no pitch or CTA inside a trading flow (size box, ticket, PLACE, alerts); the small footer byline is the only in-app placement.
- **Stream-safe:** never commit a screenshot, fixture or log with account IDs, balances, P&L or tokens. `data/` and
  `.env.projectx` are gitignored.
- Loopback by default (`127.0.0.1`); foreign Host/Origin get 403; every POST needs Origin + JSON + a CSRF header. A non-loopback
  bind refuses to start without `PX_OPERATOR_TOKEN` and `PX_ALLOWED_HOSTS` (`projectx_desk/net.py`, `docs/DEPLOY.md`); keep that refusal and its tests.
- Generate secrets, never copy them: `openssl rand -hex 32`. A weak or placeholder `PX_MCP_TOKEN` means MCP is not
  mounted.

## ProjectX facts that bite

- `Order/cancel` works on **simulated accounts only** and `partialCloseContract` is rejected on live accounts. The only
  documented live exit is an opposite-side market order, so live opens must use native OCO brackets.
- Position Brackets mode (the default) REJECTS `stopLossBracket`/`takeProfitBracket` but still creates the order.
- An ACK (`success: true`) is not a fill. Fills arrive on the user hub.
- `PositionType` Short = 2 and the `live` data-subscription flag are unverified until a real sim run confirms them.
- Rate limits: `retrieveBars` 50/30 s, everything else 200/60 s; `PXClient` stays under both.

## Gotchas

- The guard pin rejects ANY `.write` attribute access outside `guard.py` (it cannot tell a file from `PXClient`). Not even `os.write`. Write files with `print(..., file=f)`, `pathlib.Path.write_text` or `json.dump`, never a bare `.write(`, in new modules.
- Smooth synthetic bars always give ADX of about 30, so tests inject ADX/ATR-percentile values.
- The mock gateway gives every instrument the same tiny ATR, so sizes look identical across NQ and MNQ there.
- The headline contract count can be capped by `PX_MAX_QTY_ORDER`; read `risk_allows`/`cap` in the WHY lines.
