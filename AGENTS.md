# AGENTS.md: ProjectX Desk

Guidance for AI coding agents (Codex, Claude Code, others) and for humans. Read `docs/DECISIONS.md` before changing
the guard: every rule below has a reason there.

## Run and test

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                      # offline: runs against the mock gateway; no broker, no credentials. Python 3.10+
./run_dev.sh                   # offline desk + mock gateway: http://127.0.0.1:8613/?ai=1
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
| `projectx_desk/respect.py` | which levels each timeframe (5m/15m/1h/4h/1d) respects: ~25 candidate levels scored against a calibrated placebo, with a no-lookahead guarantee and a half/half stability check. Pure, no I/O |
| `projectx_desk/flow.py` | real buy/sell volume from the trade tape (`GatewayTrade.type`: 0 buy, 1 sell), 1-minute buckets per contract. Pure. No tick history exists, so coverage starts when the desk subscribed (`since`); minutes touching a reconnect gap or a bad trade are `partial` and never drawn. Unknown side is counted apart, never guessed. Persisted to `data/flow.json` (`PX_FLOW_FILE`) with holes for any time the tape was not received. Served at `/api/flow`, drawn as the BUY/SELL VOL pane, summarised in the digest as `FLOW:` |
| `projectx_desk/anchor.py` | pure level-anchored stop and target (respected levels only, never GEX), plus `projection` (room left if the stop fills) and `heat`. Fails closed. See Round 6 |
| `projectx_desk/gex.py` | GEX levels from the TraderDaddy API converted to futures price space: display and digest only, never sizing. `PX_GEX_KEY`, `PX_GEX_URL`, `PX_GEX_MAX_AGE_S` |
| `projectx_desk/ticks.py` | tick bars (N trade prints, `PX_TICK_BARS`) built from the live tape with buy/sell volume; no history, in memory |
| `projectx_desk/feed.py` | analyst futures callouts from TraderDiscord-v2 `GET /analyst-callouts` (`PX_FEED_URL`, `PX_FEED_KEY`, `PX_FEED_CHANNELS`, `PX_FEED_HOURS`; unset URL = off). Third-party text is untrusted: only allowlisted names/enums/numbers leave the module, never message text. Opinion, not signal; no path to the guard. State key and digest lines are `callouts`/`CALLOUTS` (`feed` is the market-data health block) |
| `projectx_desk/tradeday.py` | the one definition of the trading day (17:00 CT roll) |
| `projectx_desk/sizing.py`, `projectx_desk/gex_futures.py` | pure sizing math; GEX levels converted to futures price space |
| `projectx_desk/dev_gateway.py` | a mock ProjectX gateway (REST + SignalR) for offline development and end-to-end tests |
| `projectx_desk/static/` | the desk UI (vanilla JS, no build step) |

## Rules with teeth

- **Topstep rules live in `docs/TOPSTEP_RULES.md`** with sources and a per-rule status. `PX_FLAT_BY` (auto-flatten, refuse opens in the last 2 minutes) and `PX_MLL_FLOOR` (drawdown room = live balance minus the firm's floor; unreadable balance refuses) are enforced by the guard; do not weaken either. Orders must originate from the trader's own device (Topstep API terms): never document or ship a cloud-VPS deployment as the recommended path.
- **Only `projectx_desk/guard.py` may name an order-write endpoint or call `PXClient.write`.** Pinned by AST in
  `tests/test_px_guard_pin.py` (attribute access, getattr, string literals). Do not narrow the pin or add a second
  order path. MCP and everything else may only `preview` and `halt`.
- **Kill switches default OFF** (`PX_TRADING=0`, `PX_MCP_PLACE=0`). A zero or blank money or quantity limit refuses opens, as does
  a blank account id, `PX_NO_NEW_AFTER` or `PX_BRACKET_MODE` (never guess: a wrong guess still creates a rejected order). The two
  behavioral rails are the exception: `0` turns them off, blank or garbled falls back to the default.
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
- **Sizing is always computed** and rounds down. 1R is `risk_pct` of the live drawdown room (balance minus `PX_MLL_FLOOR`), never of the account
  balance and never above the remaining daily or drawdown room; unreadable inputs size to 0 (`PX_RISK_BASE`, `docs/DECISIONS.md` Round 4). ADX/ATR scalers and the behavioral rails only ever refuse or reduce.
- **Stops only tighten** (`move_stop`): toward price, never wider, never into the market, simulated accounts only.
- **The AI surface is an allowlist** of numbers, enums and validated names. No account id, no balance, no gateway free
  text: a hostile contract name or order tag must never become a prompt.
- **Nothing in the browser can tell an AI extension's click from a human's.** The 600 ms hold is anti-fat-finger, not
  security. Real controls are server-side caps, the hashed single-use ticket, the fresh-state gate, qty cap 1 and
  sim-first.
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

- Smooth synthetic bars always give ADX of about 30, so tests inject ADX/ATR-percentile values.
- The mock gateway gives every instrument the same tiny ATR, so sizes look identical across NQ and MNQ there.
- The headline contract count can be capped by `PX_MAX_QTY_ORDER`; read `risk_allows`/`cap` in the WHY lines.
