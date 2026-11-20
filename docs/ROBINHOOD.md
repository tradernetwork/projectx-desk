# Robinhood (phase 0: read-only)

Phase 0 reads one Robinhood account through Robinhood's own Trading MCP and shows it in the digest and four GET routes.
It cannot place, cancel or change anything: the client refuses every tool that is not on a fixed read list before a
byte is sent, and `tests/test_px_guard_pin.py` pins every Robinhood write tool name to the guard modules. Order
placement is phase 1 and does not exist yet. Design: `docs/superpowers/specs/2026-11-17-robinhood-broker-design.md`.

## Read this first: the risk statement

- **There is no paper trading on Robinhood.** Every order in phase 1 will be a real order with real money. The
  futures desk has a simulated account to rehearse on; this broker does not.
- **Trade approvals must be ON before phase 1.** Robinhood can require a human to approve each agent order. The desk
  reads that setting (`approval_setting` in `/api/rh/state`) and phase 1 will refuse to place anything while it is off
  or unknown. Turn it on in the Robinhood app now.
- **Use a dedicated, small account.** The MCP only exposes the account you flagged for agent use. Fund it with money
  you can afford to lose entirely. Every other account is dropped before it reaches state, digest or the Ask agent.
- The tool shapes in the mock are modelled on public fixtures and are UNVERIFIED against the live service until you
  run `scripts/rh_capture.py` and compare.

## Try it with no account (demo)

```
./run_dev.sh      # or run_dev.ps1: sets PX_RH=1 PX_RH_MOCK=1, starts a mock Robinhood MCP in-process
curl http://127.0.0.1:8613/api/rh/state
curl 'http://127.0.0.1:8613/ai/digest' | grep '^RH'
```

Every number, symbol and id from the mock is invented. The mock lists write tools (so the drift check has something to
compare) but refuses to run them.

## Real setup

| Variable | Meaning |
|---|---|
| `PX_RH` | ON by default (unset or blank is on). `0`, `false`, `no` or `off` turns the whole feature off: nothing is built, no outbound traffic. On with no token file the desk makes no call and the panel says it is not connected. Placing orders is a separate switch, `PX_RH_TRADING`, which stays `0` by default. |
| `PX_RH_TOKEN_FILE` | Token file, default `data/rh_tokens.json` (mode 0600, gitignored). |
| `PX_RH_POLL_S` | Poll interval, default 30, floor 10. |
| `PX_RH_CALLBACK_PORT` | Fixed loopback port for the sign-in redirect (headless hosts). Default: ephemeral. |
| `PX_RH_URL` | MCP endpoint. Default is Robinhood's. https only; loopback http is allowed for the mock. |
| `PX_RH_MOCK` | Dev only: serve the mock in-process with a pre-issued token. |

The desk polls only when a token file exists. In weekend quiet it stops only when the last snapshot was fully read and
shows no working order and no pending approval; a missing snapshot or any unknown section keeps it polling.

### Sign in on this machine (localhost)

Run `python scripts/rh_capture.py`. With no token file it opens the Robinhood sign-in in your browser, catches the
redirect on `localhost` (an ephemeral loopback port), saves the token pair (atomic, 0600) and writes the tool list.
Then restart the desk (`PX_RH` is on by default; set it to `0` only to switch the feature off).

### Sign in on a headless host (for example venus)

The redirect lands on the machine that started the login, so tunnel it from the laptop:

```
# on the host
PX_RH_CALLBACK_PORT=8765 python scripts/rh_capture.py      # prints the sign-in URL
# on the laptop
ssh -L 8765:localhost:8765 host
# open the printed URL in the laptop browser; the redirect travels through the tunnel
```

The token file stays on the host. Never copy it to a second machine: refresh tokens are single use and rotate, so two
holders break each other. It is UNVERIFIED that Robinhood's client registration accepts a fixed redirect port.

A refresh failure puts Robinhood into read-down (`connected: false`) and asks for a new sign-in. It does not halt
anything, because a failed sign-in creates no exposure.

## What the desk exposes

- `GET /api/rh/state`, `/api/rh/positions`, `/api/rh/orders`, `/api/rh/approvals`. `{"enabled": false}` when off.
  `?stream=1` strips dollar values.
- `RH:` lines in `/ai/digest` and an `rh` key in `/ai/state`: counts, symbols, quantities, approval counts. No account
  number, balance or provider text.
- Phase 1 order routes (below), only when `PX_RH` is on. `PX_RECORD_ONLY` registers none of this, no POST included.

## Phase 1: the order guard (`rh/guard.py`) and its routes

Equities only, whole shares, limit orders, buy to open and sell to close up to the held quantity, one working or pending
order per symbol. A fill is unprotected: the desk owns no stops. A write is sent only when Robinhood's trade-approvals
setting reads ON at the commit point, so the final tap is yours in the Robinhood app. Robinhood has no paper account.

| Env | Default | Meaning |
|---|---|---|
| `PX_RH_TRADING` | 0 | kill switch; blank or garbled is 0 |
| `PX_RH_MAX_NOTIONAL`, `PX_RH_MAX_QTY` | unset | zero, blank or garbled refuses every order (`cap_unset`) |
| `PX_RH_TICKET_TTL_S`, `PX_RH_QUOTE_MAX_AGE_S` | 120, 5 | ticket life, quote freshness |
| `PX_RH_HALT_FILE`, `PX_RH_AUDIT`, `PX_RH_STATE_FILE` | `data/` | separate from ProjectX's halt and audit |

A reply that is not clearly `awaiting_approval` halts Robinhood (`live_order`); a transport error after the send is UNKNOWN
(`place_unknown`), halts, and is never resent. Only a human clears the halt.

After a place the ticket is `awaiting_approval`. `Guard.track()` polls Robinhood's approvals and orders every 15 s (backing
off to 60 s on errors) and moves it to `approved`, `declined`, `expired` or `filled`; declined and expired free the symbol
and the notional budget. An UNKNOWN ticket is looked up by its `ref_id` with `reconcile`; if nothing is found it stays
UNKNOWN and the halt stays until you clear it. Tickets survive a restart (`PX_RH_STATE_FILE`).

### Routes, cancel and release

All POSTs use the desk's Origin + JSON + CSRF gate and answer `{"ok": false, "code": "<fixed word>"}` on a refusal, never
provider text. `GET /api/rh/tickets` lists tickets and the halt word (`?stream=1` drops prices).

| Route | What it does |
|---|---|
| `POST /api/rh/preview` | `{symbol, side, qty, limit_price}` returns a hashed ticket |
| `POST /api/rh/place` | `{ticket_id, confirm_hash}`; the hash is required |
| `POST /api/rh/cancel` | `{ticket_id}` cancels an awaiting, approved or placed order; works while halted; one attempt |
| `POST /api/rh/discard` | `{ticket_id}` drops a previewed (unplaced) ticket so its symbol frees up; nothing is sent to Robinhood |
| `POST /api/rh/reconcile` | `{ticket_id}` looks an UNKNOWN ticket up at Robinhood by its ref id; never resends, never clears the halt |
| `POST /api/rh/release` | `{ticket_id}` human only: frees a ticket whose order and approval both vanished at Robinhood (it checks first) |
| `POST /api/rh/halt/resolve` | human only: clears the Robinhood halt |

Neither the desk's MCP, the Ask box nor the desk agent can reach any of them. A cancel that gets no clear answer is UNKNOWN
and halts (`cancel_unknown`); look at the Robinhood app, then `reconcile` or `release`. Nothing resends a write.

## Phase 1: using the panel

Click **Robinhood** in the switch at the bottom left of the desk (it appears unless `PX_RH=0`). The panel shows the account
type and state (never the number), your trade-approvals setting, positions, option positions (read only), working orders, pending
approvals and every ticket with a plain-words line for its state. In stream mode (`body.streaming`) dollar values are not drawn.

1. Type a ticker, choose Buy to open or Sell to close, whole shares and a limit price, then **Preview**. A refusal is explained in
   plain words (every guard code has a line). A good preview shows the ticket code (hash8), a countdown and the warning: no paper
   trading, a fill is unprotected, approve in the Robinhood app.
2. **Discard** frees the symbol. Otherwise press and hold **PLACE** for 600 ms (a click does nothing; the ticket must have been on
   screen 1.5 s). The order goes to Robinhood and waits for your tap in the Robinhood app.
3. The ticket moves to approved, declined, expired or filled on its own. Cancel, Reconcile, Release and Clear halt show only where the
   state allows them and each asks once. An UNKNOWN ticket means the outcome is unclear and Robinhood is halted: look at the Robinhood
   app, then Reconcile, or Release when the app shows nothing.
4. `PX_RH_DEMO=1` (set by `run_dev.sh`) shows "DEMO: mock Robinhood, no real account". It is a label, not a safety: the mock cannot place.

The Ask box agent cannot read `data/rh_*` or the Robinhood halt file, and cannot reach any Robinhood route.

### Screenshots

![Robinhood panel against the offline mock: DEMO banner, account line, positions, read-only option positions, working orders and a pending approval](img/ui-robinhood-demo.jpg)

![The order ticket: the permanent warning (no paper trading, real money, a fill is unprotected, approve in the Robinhood app), the entry form and the ticket list](img/ui-robinhood-ticket.jpg)

Both are the offline mock (`./run_dev.sh`) with invented symbols (AAA, BBB, CCC); no real account was involved. `python scripts/screenshots.py`
(offline mock only, needs Playwright) also has a Robinhood scene for regenerating them.

## Leveraged ETF desk

The panel opens on a **LETF desk** view (the **Account** tab is the tables above). It lists about 700 leveraged ETFs, picks one, and
draws two charts side by side: the underlying on the left and the fund on the right, with a strip of numbers under them. It is
display only. A click on a fund only fills the ticker box of the order ticket (buy to open, no limit, no size); the ticket flow
(preview, ticket code, hold to place, your tap in the Robinhood app) is exactly the one described above and nothing in the LETF
view can start it. Inverse funds are bought like any other stock: you buy the fund.

**The fund list.** `projectx_desk/rh/letf_data.json` is a vendored copy of github.com/mphinance/leveraged-etfs (574 long and 120
short funds with leverage of 1.5x or more either way, across 315 underlyings, ranked by 20-day average dollar volume). The date it
is "as of" is shown under the list; it turns amber after 14 days and reads stale after 45. The desk never downloads it. To refresh:

```
.venv/bin/python scripts/letf_sync.py            # download, validate, rewrite the JSON (add --as-of YYYY-MM-DD for a new date)
.venv/bin/python scripts/letf_sync.py --check    # exit 1 when the committed file differs from the source
.venv/bin/python scripts/letf_sync.py --strict   # exit 1 when a new underlying label has no proxy decision
```

The three CSVs are untrusted text: they are read into memory, size capped, redirects refused, every field checked against a strict
pattern, and any failure rejects the whole run and leaves the old file in place. Dollar volume and leverage are the source's; the
desk does not recompute them.

**Which chart is the underlying.** A real ticker (QQQ, NVDA) charts itself. A label that is not a ticker is charted through a proxy
ETF from `projectx_desk/rh/letf_proxies.py`, and the chart header says so: "Gold, charted via GLD" is exact; "Crude oil, charted via
USO (approximate)" says the proxy holds futures while the fund tracks an index. A crypto, commodity, currency or volatility label
never charts itself even when it looks like a ticker. Where there is no clean proxy the fund chart is shown alone and the beta,
decay and expected-move figures read NA. Every figure that compares the fund with a proxied underlying is approximate; the screen
says so.

**The numbers under the charts** (every one shows its sample size; unknown is NA, never 0):

| Figure | What it is |
|---|---|
| Underlying today, Fund today | last price against yesterday's close. Yesterday's close is the quote's own previous close, else the last completed daily bar (skipping a bar that is the quote's own session, so a weekend reads right) |
| Expected Lx, Gap | L times the underlying's move, and the fund's move minus that, in bps. A fund move over 50% that L times the underlying does not explain reads NA ("corporate action?") instead of a fake gap |
| Realized leverage | slope of the fund's daily returns on the underlying's over the last 20 sessions, with R2. NA under 20 paired sessions |
| Volatility decay | `-L(L-1)/2 x sigma^2` per day, times 252 a year, from the underlying's last 20 daily returns. Arithmetic, not a forecast, and it leaves out fees, financing and tracking error. An inverse fund decays faster (L = -3 gives 6 sigma^2, L = 3 gives 3) |
| 20d $ volume | the source's figure. Under $5M a day is "thin": faded in the list, wide spreads, watch only |
| Spread | live (ask - bid) over the mid, in bps, with the quote's age |

**Live data.** Bars come from `get_equity_historicals` (regular hours, split adjusted; only completed bars are returned, so the bar
in progress is built from 2 second quote samples and replaced when the real one arrives; the screen says so). Daily bars are fetched
once per selection for the analytics. When the market is closed the desk reads once and stops polling. The reads are market data:
no account is resolved and none of it reaches the digest, the desk MCP or the Ask agent.

**Routes** (GET only; present only when `PX_RH` is on and never in a recorder; a refusal is `{"ok": false, "code": ...}` and nothing
else): `/api/rh/letf` (the list, no outbound call), `/api/rh/bars?symbols=QQQ,TQQQ&tf=5m` (up to 2 symbols; `tf` is 1m, 5m, 15m, 30m,
1h or 1D), `/api/rh/quotes?symbols=QQQ,TQQQ` (up to 4). Only symbols in the vendored list (funds and their chart symbols) are
accepted. Chart reads share the Robinhood client's 4 calls a second with the order guard, so they are held to 2 a second (a burst of
4); past that the route answers `busy` and the page retries. Replies are cached for 15 to 1800 seconds by timeframe (quotes 2).

**Keys** (only while the LETF view is on screen): Up and Down move through the list on the side you are on, `L` and `S` jump to the
long or short pick of the same underlying, `1` to `6` pick the timeframe, `/` searches, Enter fills the ticker box. The URL hash
`#letf=TQQQ&tf=5m` is the only memory: share it and the desk opens straight to that fund.

**Not verified yet.** The live shape of `get_equity_historicals` was captured once (2026-11-20, QQQ and TQQQ, 5 minute bars) and the
bar view matches it, but the mock is still invented data. `previous_close` inside a live quote is UNVERIFIED (the desk falls back to
the daily bars when it is absent); run `scripts/rh_shapes.py` against the live service to capture both.

## Verified against the live Trading MCP (2026-11-17)

Signed in with this desk's own OAuth code (`scripts/rh_capture.py`, localhost) and read the real server. Checked:

- **Login works** through this repo's Python port: dynamic client registration, PKCE with a loopback redirect, token saved at mode 0600.
- **96 tools** listed (`tests/fixtures/rh/tools_live_2026-11-17.json`, names and schema structure only). All 11 read tools the desk calls exist with the
  inputs it sends. Every non-`get_` tool is pinned as a write (`test_every_live_rh_write_tool_is_pinned`), so a new tool breaks the test until someone decides.
- **Read shapes** (checked with `scripts/rh_shapes.py`, which prints types and never values): `get_accounts` returns `{accounts: [...]}` with an
  `agentic_allowed` boolean; positions, orders and approvals come back as `{positions|orders|approvals: [...]}`; `get_trade_approval_setting`
  returns `{setting: {human_must_approve_trades: bool}}`; `get_equity_quotes` has `venue_bid_time`, `venue_ask_time` and `venue_last_trade_time` and
  no `updated_at`. The desk's reader and one full poll run clean against the live account (positions, orders and approvals empty on that account).
- **Bug the live check found:** the guard measured quote age from an `updated_at` field that the real quote does not have, so every order would have
  been refused `quote_stale`. It now uses the newest of the three venue times (fixed, tested, mock matches the live shape).
- **The agent-enabled account had `human_must_approve_trades: false`** (approvals OFF) when checked. The guard refuses with `approvals_off`, which is the
  intended brake: turn the setting on in the Robinhood app before any order can leave this desk.

Still unverified (nothing has been sent): the reply shapes of `review_equity_order`, `place_equity_order` and `cancel_equity_order` (the tool text says
`review` returns `customer_approval_required` and `place` takes an optional `ref_id`), whether Robinhood echoes that `ref_id` on orders and approvals,
the shape of a non-empty approvals list, how long a pending approval lives, and whether a fixed callback port (`PX_RH_CALLBACK_PORT`) is accepted for
the headless `ssh -L` route.

## Tool drift

```
python scripts/rh_capture.py --out tools-new.json
python scripts/rh_drift.py data/rh_tools.json tools-new.json     # exit 1: a tool the desk calls was removed or gained a required input
```

The capture holds tool names, descriptions and input-schema structure only, never account data.

---

Part of the Momentum Phinance toolchain. Built with [TraderMatrix](https://tradermatrix.pro?ref=MPHINANCE). The link carries a referral code; it costs you nothing.
