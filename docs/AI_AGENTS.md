# Connecting an AI agent

The desk gives an agent three things: a plain-text digest of everything it knows, an MCP server with read and sizing tools,
and the ability to **stage** a ticket for a human to review. It does not give an agent a way to place, flatten, cancel or
clear a halt. Read [LIMITATIONS](LIMITATIONS.md) for the honest boundary (an agent with a shell on the same machine is not
bounded by the MCP server).

## Connect over MCP

The MCP server is off until you set a strong token (32+ characters, no placeholder words). It mounts at `/mcp/` on the desk's
port. The trailing slash matters.

```bash
export PX_MCP_TOKEN=$(openssl rand -hex 32)
./run_dev.sh            # on the mock; run_dev.sh makes a throwaway token for you and prints the connect command
```

Claude Code:

```bash
claude mcp add --transport http projectx http://127.0.0.1:8613/mcp/ --header "Authorization: Bearer $PX_MCP_TOKEN"
```

Any MCP HTTP client (change the port if you set `CHART_PORT`):

```json
{ "mcpServers": { "projectx": { "type": "http", "url": "http://127.0.0.1:8613/mcp/",
  "headers": { "Authorization": "Bearer <PX_MCP_TOKEN>" } } } }
```

Foreign Host or Origin headers get a 403, so clients must use `127.0.0.1:<port>` or `localhost:<port>`.

## Tools

| Tool | What it does |
|---|---|
| `get_digest`, `get_state` | the whole desk as text or JSON |
| `get_respect` | which levels each timeframe has been respecting (history, not a forecast); read-only |
| `get_positions`, `get_orders`, `get_quote`, `get_bars`, `search_contracts`, `get_risk_status` | reads |
| `size_trade` | what size the desk would allow for an idea; stages nothing, always rounds down |
| `preview_order` | **stages** a ticket for a human to review and place. Returns the readback or the checks that failed. |
| `halt` | freezes new opens. Only a human can clear it. |

`search_contracts` returns only `id`, `name` (an uppercase code, or one derived from the id), `tick` and `tick_value`. `get_quote`,
`get_bars` and `search_contracts` emit only finite numbers and timestamps the desk re-formats, never raw gateway text. `preview_order`
is refused (`ui_ticket_pending`) while a ticket staged at the desk is waiting.

There is no place, flatten, cancel or clear-halt tool, and the tool list is pinned by a test.

## Plain HTTP

`GET /ai/digest` (text), `GET /ai/state` (JSON), `GET /llms.txt` (what an agent may and may not do). Query:
`?contract=CON.F.US.MES.Z26&side=long&risk_pct=0.03&stop_k=1.5` (`&stream=1` removes dollar figures). Without `contract`,
most price and sizing lines read `NA`.

## Reading the digest

One idea per line, fixed key order, `KEY: k=v`, enums uppercase, units suffixed (`pt` points, `t` ticks), `NA` for unknown.
`NA` means unknown, not zero. Stale or missing data (`FEED` down, `seeded=NO`, large `age_s`) means do not suggest an entry.

Key lines: `MODE`, `FEED`, `CONTRACT`, `QUOTE`, `VWAP`, `SESSION`, `IND` (ATR, ADX), `EMA`, `LEVEL` (reaction history with a
random-line baseline), `SIZE_*` (the size and `SIZE_WHY` reasons), `TICKET`, `KPI`, `BEHAVIOR`, `MANAGE` and
`MANAGE_SUGGEST` (open R and stop suggestions), `TRADE`, `RISK_*`, `FLOW` (real buy and sell volume from the trade tape, with `INCOMPLETE` where the tape was not being received), `CALLOUTS` and
`CALLOUT_n` (analyst futures callouts: numbers and enums only, never their text), `GEX`, `POS`, `ORD`, `ACTIONS`, `RULE`. `SIZE_STOP`, `SIZE_TARGET` and
`SIZE_IF_STOPPED` show whether the stop was tucked beyond a respected level, the nearest respected level at 1R or better with its R:R, and the room left
if the stop fills.

All numbers below come from the bundled mock gateway: synthetic data, not a result.

### Idle, with `?contract=CON.F.US.MES.Z26&risk_pct=0.05`

```
PXDESK v1 asof=15:39:09CT
MODE: engine=READY trading=ON mcp_place=OFF account=SIM bracket=POSITION
FEED: market=UP user=UP seeded=YES market_age_s=0.1
CONTRACT: root=MES tick=0.25 tickval=1.25 active=YES
QUOTE: last=5000 bid=5000 ask=5000.25 age_s=0.1
VWAP: 4993.04 vs_last=+6.96pt side=ABOVE
SESSION: hi=5001.25 lo=4988.25 volume=192397
IND: atr_5m=0.9pt atr_pctile=100 adx_5m=55.89
EMA: stack=BULL 8=5000.05 21=4998.75 34=4997.51
LEVEL: EMA8 5000.05 -0.05pt BELOW tested=8 held=7 failed=1 neutral=0 hold_rate=87.5% random_lines=88%(n=25) verdict=NOT_DISTINGUISHABLE last=BOUNCE 2bars_ago
LEVEL: EMA21 4998.75 +1.25pt ABOVE tested=4 held=3 failed=1 neutral=0 hold_rate=75% SMALL_SAMPLE random_lines=82.8%(n=29) verdict=TOO_FEW_TESTS last=FAIL 78bars_ago
LEVEL: EMA34 4997.51 +2.49pt ABOVE tested=3 held=2 failed=1 neutral=0 hold_rate=66.7% SMALL_SAMPLE random_lines=73.1%(n=26) verdict=TOO_FEW_TESTS last=BOUNCE 78bars_ago
LEVEL: VWAP 4993.04 +6.96pt ABOVE tested=3 held=3 failed=0 neutral=0 hold_rate=100% SMALL_SAMPLE random_lines=66.7%(n=15) verdict=TOO_FEW_TESTS last=BOUNCE 43bars_ago
RESPECT 15m: ema21 held=7/11 hold_rate=64% placebo=41% p=0.0712 fdr=NO verdict=NOT_DISTINGUISHABLE stable=NA price=4998.75 (tested=11 family=118) SMALL_SAMPLE
RESPECT_NOTE: RESPECT is history of how price reacted to each level, not a forecast.
SIZE_INPUT: side=LONG risk_pct=5 stop_atr=1.5 budget_usd=33.5
SIZE_IDEA: entry=5000.25 stop=4998.75 stop_t=6 stop_pt=1.5
SIZE_WHY: Risk: 1R = 5% of drawdown room = $50.00 (capped by $1,000 loss room)
SIZE_WHY: Cost: $3.34 per MES round turn (2t slippage + $0.84 fees) = 44.5% of 1R; 1:1 needs 72.3% wins after costs
SIZE_WHY: ADX 55.9 >= 20: trending enough, no cut (x1)
SIZE_WHY: ATR in the 100th percentile: volatile, wide stops (x0.67)
SIZE_WHY: After cuts: $33.50 (x0.67)
SIZE_WHY: Stop: 1.5 x ATR 0.90pt = 6t = $7.50 per MES
SIZE_WHY: Risk alone allows 4; cap PX_MAX_QTY_ORDER=1 limits you to 1
SIZE_RESULT: contracts=1 risk_usd=7.5 pct_budget=0.8 binds=MAX_CONTRACTS risk_allows=4 cap=1
SIZE_R: 1R=7.5 2R=15 3R=22.5
TICKET: NONE
KPI: n=0
BEHAVIOR: consecutive_losses=0 cooldown_s=0 locked=NO size_locked=NO
TRADE: NONE
RISK_DAILY: limit=1000 room=1000
RISK_DD: buffer=2000 remaining=1000 binds=DAILY
RISK_OPEN: contracts=0/3 orders_today=0/10
GEX: status=UNAVAILABLE
GEX_NOTE: positioning not targets; flip is regime boundary
POS: NONE
ORD: NONE
FILLS_TODAY: 0 pnl_day=0
ACTIONS: stage=ENABLED place=NEEDS_TICKET
RULE: agent reads and stages; the operator places. Voice never confirms.
NOTE: GEX marks positioning, not targets; the flip is a regime boundary, not a forecast.
```

Since 2026-10-23 the digest also carries a `BIAS:` line, advisory trend context and **not a signal** (never backtested; see [DECISIONS](DECISIONS.md) Rounds 17 and 18).
The format, shown with a real read from a simulated account (not the practice market above):

```
BIAS: LONG grade=B score=2.28/3.5 agree=4/5 entry=MID 5m=+ 15m=+ 1h=0 vwap=+ flow=+ flags=VOL_HIGH
BIAS_NOTE: advisory trend context, NOT a signal (backtest showed no follow-through edge); entry=PULLBACK means price is near the 21 EMA, EXTENDED means chasing; never sizes or gates an order
```

`entry` is PULLBACK, MID, EXTENDED or THROUGH_EMA; `flags` can include DISSENT, EXTENDED, STRETCHED, EXTENDED_RSI, THROUGH_EMA, RANGING, VOL_HIGH, VOL_EXTREME, OPEN_CHOP and FLOW_NA.
An agent should read it as context and must never treat the letter as a reason to stage.

### Mid-trade (excerpt of the lines that change)

```
QUOTE: last=5004.5 bid=5004.5 ask=5004.75 age_s=0.2
TICKET: NONE
KPI: n=0
BEHAVIOR: consecutive_losses=0 cooldown_s=0 locked=NO size_locked=NO
MANAGE: LONG 1 avg=5000.25 last=5004.5 stop=4997.25 open_R=1.42 (stops only tighten)
MANAGE_SUGGEST: BREAKEVEN 5000.5 valid=YES recommended=YES (entry plus one tick)
MANAGE_SUGGEST: ATR_TRAIL 5002.5 valid=YES recommended=NO (2 ATR from last)
MANAGE_SUGGEST: EMA21_TRAIL 4998.5 valid=YES recommended=YES (a quarter ATR beyond EMA21)
TRADE: state=PLACED acked=YES side=LONG size=1 filled=1 entry_ref=5000.25 stop=4997.25 stop_state=ACKED
POS: LONG 1 CON.F.US.MES.Z26 @ 5000.25
ORD: SELL STOP 1 CON.F.US.MES.Z26 stop=4997.25 limit=NA tag=OWN
FILLS_TODAY: 1 pnl_day=-0.37
```

## Rules for agents working on this repo

Read [AGENTS.md](../AGENTS.md). In short: do not add a second order path, do not widen the MCP tool list, keep
`tests/test_px_guard_pin.py` green, never commit real credentials, account numbers, balances or screenshots of a real account.
