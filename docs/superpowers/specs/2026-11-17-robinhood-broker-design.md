# Robinhood as the desk's first personal-money broker: design

Status: DRAFT for approval. Nothing here is built. Tags: **[MICHAEL]** decided in conversation, **[CALL]** my proposal with a reason, **[UNVERIFIED]** nobody has run it.

Purpose: add Robinhood (official Trading MCP, equities and options, crypto later) to ProjectX Desk as a second broker, built so strangers can run it offline and a showcase can be recorded safely, without touching the prop-firm order path.

## Intent and why

- **[MICHAEL]** ProjectX is the prop-firm broker. Robinhood is the first broker for personal money, so the two follow different rules.
- **[MICHAEL]** The point is to show others what this desk can do. TraderMatrix Desktop (Tauri, `tradernetwork/tradermatrix-desktop`) was the earlier vehicle for Robinhood publicity and is being abandoned in favour of this desk. Its Robinhood work is folded in here.
- **[MICHAEL]** Promotion runs through tradermatrix.pro links, which keep `?ref=MPHINANCE`, with no pitch inside a trading flow (existing branding rule).
- **[MICHAEL]** Later, a broker interface justified by two real implementations, shaped like ConnectTrade (see Future shape). If a source cannot be plugged in directly, an ETL step normalises it.

## Goals

1. Anyone can clone the repo, run `./run_dev.sh`, and see Robinhood positions, options, orders and an order ticket against a **mock Robinhood MCP server**, with no account and no credentials.
2. With a real account, the desk reads the dedicated Robinhood MCP account and the Ask agent can answer about it.
3. Phase 1 can send an order only when Robinhood's own trade-approvals setting is verified ON, so the final tap is the owner's, in the Robinhood app.
4. The prop-firm path (`guard.py`, sizing, MLL, flat-by, protect-open-positions) is not modified.

## Non-goals and anti-goals

- No futures on Robinhood (the MCP has none yet; revisit when it ships).
- No password-based `robin_stocks` route (ban risk, stores credentials, breaks on app updates).
- No order cap, arm step or kill switch "by design" stance copied from TraderMatrix Desktop: the opposite is required here.
- No auto-retry of a write inside a time window (TraderMatrix Desktop retries with the same `ref_id` for 600 s). The desk rule is never replay.
- Anti-goal: a demo that could place a real order, or a screenshot that leaks balances.

## Constraints

- Robinhood has **no paper account**, so sim-first cannot hold. Mitigations: mock server for everything offline; live placement only with approvals ON; desk-side caps.
- Robinhood has **no server-side caps**; approvals are the only broker-side brake and are OFF by default for MCP accounts. The desk verifies the setting live before every write and refuses if it is off or unreadable.
- Read scope covers **all** of the user's Robinhood accounts; trade scope covers only the MCP account. The adapter drops every non-MCP account before anything reaches state, the digest or the Ask agent.
- Tool names and parameter schemas are **[UNVERIFIED]** (published list has names only). Phase 0 includes a capture script and a drift check, ported from `scripts/rh-tool-drift.py`.
- Headless OAuth on venus **[UNVERIFIED]**: tokens are single-use rotating refresh tokens, persisted atomically (`os.replace`, mode 0600) before the old one is spent. The loopback callback cannot reach a browser on venus, so first login is done on the laptop and the token file is copied, or a device flow is used if one exists.
- Existing desk rules carry over: AI surface is an allowlist of numbers, enums and validated names (no account id, no balance, no free text); stream mode hides balances; `data/` and `.env.projectx` stay gitignored; no em or en dashes; write files with `print(..., file=f)`, `write_text` or `json.dump`, never a bare `.write(`.

## Approaches considered

- **A. Generalise `guard.py` into a broker interface now.** Cleanest end state, but it rewrites the live prop-firm path (1,236 passing tests) mid-evaluation. Rejected for now.
- **B. A separate `rh/` package with its own guard.** ProjectX untouched, same safety shape, some duplication. Chosen as the base.
- **C. B delivered in phases: read and stage first, own placement later, mock server throughout.** **Chosen [MICHAEL]: "C, built on B".**

## The design

### Package layout [CALL]

All under `projectx_desk/rh/`, one module per job, pure where possible:

| Module | Job |
|---|---|
| `mcp.py` | Streamable-HTTP MCP client for `https://agent.robinhood.com/mcp/trading` (loopback or https only, redirects refused, 4 calls/s limiter, reads retry once, writes make ONE attempt and only from `guard.py`) |
| `oauth.py`, `tokens.py` | OAuth 2.1 PKCE with dynamic client registration, loopback redirect, rotating token store |
| `reads.py` | the fixed read-tool set: accounts (with the agentic-allowed flag), portfolio, equity and option positions and orders, quotes, option chains, realised P&L, trade approvals and the approval setting |
| `views.py` | allowlisted `position_view` / `order_view` style reducers into canonical-shaped dicts (see Future shape) |
| `guard.py` | THE ROBINHOOD ORDER PATH, mirroring `projectx_desk/guard.py`: preview (checks, sizing, hashed single-use ticket), place, reconcile, halt, audit |
| `digest.py` hook | `RH:` lines in `/ai/digest`, `rh` key in `/ai/state`; numbers and enums only |
| `dev_robinhood.py` | mock Robinhood MCP server (OAuth stub, accounts, positions, option chains, orders, approvals, rejections, an UNKNOWN outcome), for tests, demos and screenshots |

Pins: `tests/test_px_guard_pin.py` is **extended, not narrowed**: exactly two files may name a write endpoint or write tool (`guard.py`, `rh/guard.py`). Every Robinhood write tool name (`place_*`, `cancel_*`, `exercise_option`, `decline_trade_approval`, watchlist, alert, scanner and Legend writes) is on the pinned list. MCP (the desk's own) and everything else may only preview.

### Phase 0: foundation and demo

Mock server, `mcp.py`, `oauth.py`, `tokens.py`, `reads.py`, `views.py`, the tool-list capture and drift scripts, pin extension. Kill switches `PX_RH=0` (whole feature off, default) and `PX_RH_TRADING=0` (default). `run_dev.sh` starts the mock and a demo account.

### Phase 1: read, then stage through Robinhood approvals

- **Read:** a Robinhood panel (positions, option positions with Greeks if the MCP returns them, working orders, pending approvals, realised P&L) behind a broker switch in the header; Ask agent and the new provider picker work on both brokers.
- **Order ticket:** the desk builds a typed intent (symbol, side, quantity, limit price). `rh/guard.py` previews it: kill switches, MCP-account-only, approvals ON verified live (fresh read, unreadable refuses), market hours, caps, a hashed single-use ticket with expiry. Placing sends the order; with approvals ON the expected outcome is `awaiting_approval` and the owner taps in the Robinhood app. A reply that says the order is live (approvals not honoured) is treated as exposure: the desk halts RH and reports it.
- **Outcomes:** placed, awaiting_approval, rejected, UNKNOWN. UNKNOWN is persisted, trips the RH halt, and is reconciled by looking the order up by its `ref_id`; never resent. Only a human clears the halt (no MCP tool for it).
- **Caps [CALL]:** whole shares, limit orders only, equities only in phase 1, `PX_RH_MAX_NOTIONAL` and `PX_RH_MAX_QTY` with zero or blank refusing (fail closed like `limits.py`), one working order per symbol. Options and market orders wait for phase 2.

### Phase 2: placement not leaning on approvals (separate gate)

Only after tool schemas are captured live, a run of phase-1 orders is clean, and the owner explicitly accepts "no sim, no broker caps". Adds desk-side daily loss and notional room, option single-leg tickets, and decides whether approvals OFF is ever allowed. Not designed in detail here.

### Future shape: a broker interface and ETL [MICHAEL, deferred]

ConnectTrade is the reference layout: one canonical model across brokers, a per-broker capability matrix (equities, options, futures, crypto, paper), scoped read and trade tokens, REST plus streaming for account data and market data. When a second implementation (Robinhood next to ProjectX) exists, extract:

- canonical dataclasses: `Account`, `Position`, `Order`, `Fill`, `Quote`, plus a `Capabilities` record;
- adapters per broker that map native payloads to them; where a source cannot be plugged in live, an ETL step lands normalised rows (JSONL or SQLite, allowlisted fields, like `journal.py`) that the same readers consume;
- the guard layer keeps per-broker rule profiles (prop-firm rules vs personal-account rules) rather than one rule set.

Phase 0 builds `rh/views.py` in canonical-compatible shape so this extraction is mechanical. Nothing canonical is built now (rule of two implementations). ConnectTrade is a B2B aggregator needing platform credentials, and Robinhood is not among its 21 brokers, so it is a layout reference, not a dependency.

### Showcase and safety defaults [CALL]

- Demo mode is the default for a fresh clone: mock server, fake account, an on-screen "DEMO" banner, no outbound calls.
- Stream mode hides Robinhood balances, account numbers and P&L (extends the existing stream-safe rule); screenshots come only from the mock (`scripts/screenshots.py` gets an RH scene).
- README and docs gain a Robinhood section with the risk statement (no paper trading, approvals ON required, dedicated small account) and the `?ref=MPHINANCE` TraderMatrix link in the footer only.
- Orders originate from the owner's own machine; the Topstep "own device" restriction concerns ProjectX only.

## Left to the builder

Exact tool names and argument shapes (from the Phase 0 capture), module internals, the UI layout of the Robinhood panel, test fixtures (seed from TraderMatrix Desktop's `tests/fixtures/rh`, check its licence and keep attribution), error-code vocabulary, and how `scripts/` captures and diffs the tool list.

## Calls made after the builder check [CALL]

Each is my proposal with a reason; overrule any of them.

1. **Closing orders are in phase 1; protection is not.** The ticket can buy to open and sell to close (quantity at most the held position, limit only, same approvals and caps). The desk owns no stops for Robinhood positions and there is no protect-open-positions analogue: Robinhood stop orders exist, but a desk-held protection promise needs the live schema and a clean phase-1 run first. The ticket and panel say plainly that a filled position is unprotected.
2. **Approval state machine.** `awaiting_approval` counts toward the one-working-order-per-symbol rule and the notional cap until it resolves. The desk polls the pending approvals read (every 15 s while any is pending, backing off) and moves a ticket to `approved`, `declined` or `expired`; the last two free the caps. The desk supplies the `ref_id` and reconciles by looking the order up with it **[UNVERIFIED]**: the live capture must confirm Robinhood accepts a caller-supplied id and how long a pending approval lives. If it does not, phase 1 placement ships dark.
3. **Separate state per broker.** The Robinhood halt, audit log and day state are separate files from ProjectX's, so a halt on one never blocks the other. The one-desk-per-checkout lock stays a single lock covering both.
4. **Approvals not verifiable means no placement.** If no read tool reports the approvals setting, or it is ambiguous, the guard refuses every write and the ticket ships dark (read-only desk). The field that distinguishes a live order from a pending one is fixed by the live capture; until then any reply that is not clearly `awaiting_approval` is treated as possible exposure and halts RH.
5. **Sequencing.** The Ask-box provider work lands first (it is built and tested). Robinhood routes and the broker switch come after it. `PX_RECORD_ONLY` registers zero Robinhood POST routes, same as every other POST.
6. **The mock is invented first and aligned later.** Phase 0 is not blocked on a real account: shapes come from the TraderMatrix Desktop fixtures and the published tool names, and the capture script later reports the differences so the mock and views are corrected.
7. **Defaults.** `PX_RH` and `PX_RH_TRADING` default off for a real install. `run_dev.sh` sets `PX_RH=1` against the mock, so a fresh clone that runs the dev script gets the demo and banner, while a production `.env.projectx` stays off until the owner opts in.
8. **Equities only in phase 1.** Crypto waits; options are read-only (positions, chains, Greeks, in the digest) until phase 2.
9. **Token ownership and login [MICHAEL: venus plus laptop for the owner, localhost for most users].** One host owns the token file. Default (localhost): login opens the browser on the same machine, callback on an ephemeral loopback port, nothing else to set up. Headless host (venus): a fixed callback port (`PX_RH_CALLBACK_PORT`) and an SSH tunnel from the laptop (`ssh -L PORT:localhost:PORT venus`), so the sign-in page opens in the laptop browser and the redirect reaches the desk on venus; no token is copied and no second host ever holds a refresh token. The same tunnel finishes the OpenRouter sign-in on a headless box. A refresh failure puts Robinhood into read-down and asks for a fresh login; it does not halt, because a failed login creates no exposure. **[UNVERIFIED]** that Robinhood's dynamic client registration accepts a fixed redirect port: confirmed in the phase 0 live capture.

## Related

- `docs/DECISIONS.md` (a Round 26 entry records this decision when approved), `docs/TOPSTEP_RULES.md`
- awesome-broker-mcp `brokers/robinhood.md`, `aggregators/connecttrade.md`
- `ai-everywhere/docs/trading-agent/BROKER_SCHEMA.md` (catalog schema, not an order interface)
