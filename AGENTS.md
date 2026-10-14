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

`python px_app.py` loads `.env.projectx` first (shell variables win; only key names are printed).
Modules are flat at the repo root (`px_*.py`, `sizing.py`, `gex_futures.py`); `tests/conftest.py` puts the root on
`sys.path`. Keep the `px_` prefix on new modules.

## Layout

| File | Job |
|---|---|
| `px_guard.py` | THE ORDER PATH: preview (checks, sizing, hashed ticket), place, stop-after-fill, watchdog, flatten, cancel, move_stop, reconcile, halt, audit |
| `px_client.py` | REST. Reads retry once on 401; `write()` makes ONE attempt (guard only) |
| `px_hub.py` | SignalR client for the user and market hubs; replays subscriptions; signals resync |
| `px_state.py` | live quotes/positions/orders/trades; trading-day P&L; allowlisted `position_view`/`order_view` |
| `px_limits.py` | config (every limit fails closed) and persisted day state |
| `px_digest.py` | the AI surface: one state dict, `/ai/state` JSON and `/ai/digest` text |
| `px_mcp.py` | FastMCP tools (read, size, stage, halt). No place/flatten/cancel/clear |
| `px_net.py` | bind and exposure rules: loopback detection, host/origin allowlists, the operator login, the refusal to start unsafe |
| `px_app.py` | FastAPI: chart data, SSE ticks, CSRF-gated POST routes, mounts MCP |
| `px_levels.py` | EMA stack, ATR series, level-reaction statistics with a random-line baseline |
| `px_time.py` | the one definition of the trading day (17:00 CT roll) |
| `sizing.py`, `gex_futures.py` | pure sizing math; GEX levels converted to futures price space |
| `dev_gateway.py` | a mock ProjectX gateway (REST + SignalR) for offline development and end-to-end tests |
| `static/` | the desk UI (vanilla JS, no build step) |

## Rules with teeth

- **Only `px_guard.py` may name an order-write endpoint or call `PXClient.write`.** Pinned by AST in
  `tests/test_px_guard_pin.py` (attribute access, getattr, string literals). Do not narrow the pin or add a second
  order path. MCP and everything else may only `preview` and `halt`.
- **Kill switches default OFF** (`PX_TRADING=0`, `PX_MCP_PLACE=0`). A zero or blank money or quantity limit refuses opens, as does
  a blank account id, `PX_NO_NEW_AFTER` or `PX_BRACKET_MODE` (never guess: a wrong guess still creates a rejected order). The two
  behavioral rails are the exception: `0` turns them off, blank or garbled falls back to the default.
- **Never replay a write.** Any ambiguous outcome is UNKNOWN: persist, trip the halt file, reconcile by `customTag`.
  Only a human clears a halt (there is deliberately no MCP tool).
- **Sizing is always computed** and rounds down. ADX/ATR scalers and the behavioral rails only ever refuse or reduce.
- **Stops only tighten** (`move_stop`): toward price, never wider, never into the market, simulated accounts only.
- **The AI surface is an allowlist** of numbers, enums and validated names. No account id, no balance, no gateway free
  text: a hostile contract name or order tag must never become a prompt.
- **Nothing in the browser can tell an AI extension's click from a human's.** The 600 ms hold is anti-fat-finger, not
  security. Real controls are server-side caps, the hashed single-use ticket, the fresh-state gate, qty cap 1 and
  sim-first.
- **Stream-safe:** never commit a screenshot, fixture or log with account IDs, balances, P&L or tokens. `data/` and
  `.env.projectx` are gitignored.
- Loopback by default (`127.0.0.1`); foreign Host/Origin get 403; every POST needs Origin + JSON + a CSRF header. A non-loopback
  bind refuses to start without `PX_OPERATOR_TOKEN` and `PX_ALLOWED_HOSTS` (`px_net.py`, `docs/DEPLOY.md`); keep that refusal and its tests.
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
