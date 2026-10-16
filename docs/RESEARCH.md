# What others are building (research pass, 2026-10-16)

A survey of public GitHub projects around the ProjectX / TopstepX gateway, and four repos the owner pointed at. Everything here is
**third-party claims or observations unless a primary source is named**: none of it has been confirmed on our own account, and the desk
still has no verified order write. Ideas only: no code was copied, and several of these repos carry no licence.

## The landscape

No other public project we found combines a guarded order path, risk sizing off the loss limit, auto-flatten, a tape-built volume
view and an AI-readable digest. Closest relatives: `SharurTrading/projectx-rs` (Rust client, MIT-0, with a venue behaviour guide),
`TexasCoding/project-x-py` (the most active Python SDK, MIT), and `adammarquette/MarqSpec.Mcp.TopstepX` (a read-only MCP with CI that
proves no order path exists, the same idea as our AST pin). `harshil1502/topstepx-mcp` exposes place/modify/cancel to the AI, which is
the opposite of our design. The four repos the owner named (`topstepx-api-health-monitor`, `PropExperiment`, `projectx-trader`,
`t2-topstep-spec`) taught us little about the order path; one (`projectx-trader`) uses the web app's private backend with spoofed
desktop-app headers, which is a terms-of-service and ban risk we avoid.

## Facts that confirm or sharpen what we assumed

| Claim | Source | Status here |
|---|---|---|
| `PositionType` Short = 2 | official realtime docs; observed by projectx-rs | documented and third-party observed; still unproven on our account (a wrong value is now handled safely: an unrecognised side is flattened, never ignored) |
| Every account row reports `simulated: true`, evaluation accounts included | projectx-rs; MarqSpec says funded accounts too | so cancel and stop-modify are probably available on Combine and Express, but the flag cannot tell practice from eval |
| `Order/place` has no `isAutomated` field | official docs, fetched 2026-10-16 | **a spec repo claimed it is required by CME Rule 575; the documented gateway has no such field, so we do not send one** |
| `trailPrice` is an absolute price; `customTag` must be unique across the account | official docs | we already generate unique tags |
| Rate limits 50/30 s (bars) and 200/60 s (all else), shared by reads and writes; a failed login still returns HTTP 200 | docs; projectx-rs | matches our client |
| Hub rows can arrive out of order (a position or trade before its order) and rows missed during a drop are not replayed | projectx-rs behaviour guide | why protect/reconcile re-read REST and why a settle window may be worth adding |
| Hub trade rows can restate an older trade (`action` = 1) | projectx-rs | unverified here: our trade dedupe is by id only |
| `Order/searchOpen` omits Suspended bracket children | projectx-rs | matters only in OCO mode (flatten's cancel sweep) |
| Bars are stamped at window open; daily and longer bars align to the session open | projectx-rs; MarqSpec calendar | consistent with `tradeday.py` (17:00 CT roll) |
| Topstep: orders must come from your own device, no VPS, no API on Live Funded, HFT banned | help article 11187768 | already in `docs/TOPSTEP_RULES.md` |

## Done because of this pass

- `GatewayLogout` ("Multiple sessions detected") now halts new orders.
- `PX_EARLY_CLOSES` lets you list holiday and early-close flatten times (see `docs/LIMITATIONS.md`).
- The tape's minute bucketing by the trade's own exchange timestamp is pinned by a boundary test (a vendor in the research buckets by
  receive time instead).
- `canTrade` as a preview check already existed (`account_tradable`).

## Open ideas, in rough priority (none started)

1. A rate budget reserved for exits (about 50 of 200 per minute) so reads can never starve flatten, cancel and stop writes.
2. In OCO mode, search orders including Suspended children and include them in flatten's cancel sweep.
3. Ignore restated trade rows (`action` = 1) and add `commissions` to fees. Verify on a sim run first.
4. A settle window (about 5 s) before declaring a reconcile mismatch or `EXTERNAL_UNPROTECTED`, because hub rows arrive out of order.
5. Session-aware staleness for GEX and levels (our `PX_GEX_MAX_AGE_S` is wall-clock, which misleads across weekends and holidays).
6. A boot-time hash of the effective limits and a warning when `.env.projectx` changes after boot; a loud alert when the hub drops with
   a position open or protection is slow; a pre-trade readiness panel (token age, last-bar age, `canTrade`).
7. Learning-loop discipline for the scanner: a minimum occurrence count and a cap on changes per cycle.
8. Mistakes seen elsewhere to avoid: retrying order writes on 5xx (our `write()` makes one attempt), a drawdown check that never reads
   the real MLL floor, expired default contracts, and publishing operator details (live account, drawdown room) in docs.
