# Using it with a real account

Not yet verified against the real gateway (see [LIMITATIONS](LIMITATIONS.md)). Go slowly, start on a simulated account,
and expect to find rough edges. Your firm's rules on automation and API use are yours to check: a violation costs the
account, not a stop-out.

## 1. Configure

```bash
cp .env.projectx.example .env.projectx     # gitignored; never commit it
$EDITOR .env.projectx
python px_app.py                           # loads .env.projectx automatically; existing shell variables win
```

At minimum set `PROJECTX_USERNAME`, `PROJECTX_API_KEY`, `PX_ACCOUNT_ID`, and leave `PX_ACCOUNT_KIND=sim`.

The API logs in with a **pair**, not the key alone. `PROJECTX_USERNAME` is the username you sign in to the firm's platform with
(not your email, not an account name or id). The key-generation screen does not show it, so use your login name. A wrong username
fails the same way a wrong key does (`AUTH_FAILED`). `PX_ACCOUNT_ID` is the numeric id of the one account the desk may touch; until
it is set, every order is refused, but you can still read. The desk talks to
exactly one account and refuses any other. Money and quantity limits fail closed: a zero or blank value refuses opens, and so does a blank account id, bracket mode or
`PX_NO_NEW_AFTER`. The two behavioral rails are different: `0` turns that rail off, and a blank or garbled value falls back to
its default.

| Setting | What it means |
|---|---|
| `PX_DAILY_LOSS_LIMIT`, `PX_DRAWDOWN_BUFFER` | dollars of room you type in. The desk takes the smaller of daily room and buffer as your budget. It does not read your firm's numbers. |
| `PX_BRACKET_MODE` | `position` (stop sent after the fill, simulated accounts) or `oco` (native atomic bracket). Must match your account's setting. Blank refuses opens. |
| `PX_NO_NEW_AFTER` | `HH:MM` Central Time, at or before 17:00. Blank refuses opens. |
| `PX_MAX_QTY_ORDER` / `_SYMBOL` / `_TOTAL` | contracts per order, per symbol, in total (default 1 / 2 / 3). |
| `PX_ALLOWED_ROOTS` | instruments you may open (default micros: MES, MNQ, MCL, MGC). |
| `PX_COOLDOWN_AFTER_LOSS_S`, `PX_MAX_CONSEC_LOSSES`, `PX_SIZE_AFTER_LOSS_LOCK` | the behavioral rails. |
| `PX_MAX_ORDERS_DAY` | orders per trading day (default 10). The day rolls at 17:00 Central. |
| `PX_ALLOW_LIVE`, `PX_ACCOUNT_KIND` | a live account needs `PX_ACCOUNT_KIND=live`, `PX_ALLOW_LIVE=1`, `PX_BRACKET_MODE=oco` and market entries. Never run. Do not enable. |
| `PX_RISK_BASE` | `drawdown` (default): 1R is `risk_pct` of the live drawdown room (balance minus `PX_MLL_FLOOR`), capped by the daily and drawdown room. `remaining` restores the old base, `risk_pct` of the smaller of the two rooms. |
| `PX_SIZE_SCALERS` | `0` switches off the ADX/ATR risk cuts (default on). |
| `PX_LIVE_DATA` | `1` asks for the live data subscription on bars; unverified. |
| `PX_MCP_TOKEN` | enables the MCP server when strong (32+ characters, no placeholder words). |
| `PX_TRADING` | `0` (default): the desk can read, size, stage and flatten, but `place` is refused. `1`: placing is allowed, subject to every other check. |

## 2. Look before you trade

Start with `PX_TRADING=0`. Open the desk, check that positions, orders and balance match your platform, watch the digest
(`/ai/digest?contract=<id>`), and stage tickets to see the readback without sending anything. Only then set `PX_TRADING=1`
on a simulated account.

## 3. Rehearse on a simulated account

Work through the checklist in [DECISIONS.md](DECISIONS.md) ("v1 can place a sim order"). The unverified items in
[LIMITATIONS](LIMITATIONS.md) are the things to confirm, and the most useful thing you can do is report what the real gateway
did. Keep the platform open next to the desk and watch each order, fill, stop and flatten land.

## 4. Graduation

Do not raise the quantity cap because it felt fine once. A reasonable bar: dozens of simulated round trips with no UNKNOWN
state left unreconciled, no orphan stops, and FLATTEN tested including after a forced failure (the mock has switches for
that: `dev_gateway.py`).
