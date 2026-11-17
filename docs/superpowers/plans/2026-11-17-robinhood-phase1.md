# Robinhood Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** a Robinhood order ticket that can only send when Robinhood's trade-approvals setting is verified ON, with a pending-approval state machine, a Robinhood panel and a demo-safe UI, all runnable against the mock.

**Architecture:** `projectx_desk/rh/guard.py` is the Robinhood order path (the second pinned writer), mirroring `projectx_desk/guard.py` in shape: preview with checks, hashed single-use ticket, place, reconcile, halt, audit, with separate halt/audit/day-state files. The mock grows write behaviour. UI is a new `static/rh.js` panel behind a broker switch.

**Tech Stack:** Python, FastAPI, vanilla JS. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-17-robinhood-broker-design.md` (sections Phase 1 and "Calls made after the builder check"). Phase 0 plan: `docs/superpowers/plans/2026-11-17-robinhood-phase0.md`.

## Global Constraints

- Only `projectx_desk/guard.py` and `projectx_desk/rh/guard.py` may name a Robinhood write tool (`place_equity_order`, `cancel_equity_order`, `review_equity_order` is a read-side preview but is treated as write-adjacent and also guard-only) or call the client's write path. `tests/test_px_guard_pin.py` stays pinned; never narrow it.
- `PX_RH_TRADING` default 0 (blank or garbled = 0); `PX_RH_MAX_NOTIONAL` and `PX_RH_MAX_QTY` zero or blank refuse every order (fail closed, like `limits.py`).
- Equities only, whole shares, LIMIT orders only. Buy to open, sell to close with qty at most the held position. One working or pending order per symbol. The desk owns no stops; the ticket and panel say a fill is unprotected.
- A write needs, fresh at the commit point after the awaited reads: kill switch on, MCP account resolved, `approval_setting` readable and ON, ticket unexpired and single-use, quote fresh, market hours, caps. Anything unreadable refuses.
- Never replay a write. Any ambiguous outcome is UNKNOWN: persist, trip the RH halt, reconcile by `ref_id` lookup; only a human clears the halt (no MCP tool, no auto-clear). RH halt, audit (`data/rh_audit.jsonl`, `PX_RH_AUDIT`), halt file (`PX_RH_HALT_FILE`) and day state are separate from ProjectX's; one process lock covers both.
- A reply that is not clearly `awaiting_approval` (the live-order case) is treated as exposure: halt RH and report it.
- `PX_RECORD_ONLY` registers no Robinhood POST route. All POSTs use the existing Origin + JSON + CSRF gate. The AI surface stays an allowlist and the desk's own MCP gets no Robinhood write tool.
- Stream mode hides Robinhood prices, balances and P&L in the UI too. No em or en dashes. No bare `.write(` outside guard modules. Do not touch `static/desk.js` (owner's uncommitted edits); add the panel through a new file plus one script tag.
- `pytest -q` fully green before each commit; commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- Approvals setting flips OFF between preview and place: place refuses and audits `approvals_off`. (Task 1)
- Two tickets for the same symbol, or a sell larger than the position, or a buy while an approval is pending: refused. (Task 1)
- Place returns a transport error after the request was sent: UNKNOWN, halt, never resent; reconcile finds the order by `ref_id` or stays UNKNOWN. (Task 2)
- Approval declined or expired in the app: ticket moves to `declined` or `expired` and the caps free up. (Task 2)
- A page, callout or Ask-agent text must never be able to place: no Robinhood write tool is reachable from the desk's MCP or from `/api/chat`. (Task 3)

---

### Task 1: Guard core (preview, ticket, place)

**Files:** Create `projectx_desk/rh/guard.py`, `projectx_desk/rh/limits.py`; Modify `projectx_desk/rh/mcp.py` (a guard-only write path: `Client.submit(tool, args, *, _guard_key)` where `_guard_key` is a module-private object only `rh/guard.py` can construct; reads still refuse write tools); Modify `projectx_desk/dev_robinhood.py` (write behaviour: `review_equity_order` returns a review with `ref_id`, `place_equity_order` returns `awaiting_approval` when the mock's setting is ON or `placed` when OFF, `cancel_equity_order`, knobs to approve/decline/expire/fail a pending approval and to drop the reply); Test `tests/test_rh_guard.py`.

**Interfaces:**
- Produces `limits.RhLimits.from_env(environ) -> RhLimits` (fields `trading: bool`, `max_notional: float|None`, `max_qty: int|None`, `ticket_ttl_s: int` default 120, `quote_max_age_s` default 5; zero/blank money or qty = None meaning refuse).
- Produces `Guard(reader, client, limits, audit_path, halt_path, state_path, clock=time.time)` with `async preview(intent: dict) -> Ticket` (intent keys `symbol`, `side` buy|sell, `qty`, `limit_price`; raises `Refused(code, detail)` with fixed codes `trading_off|halted|account_unknown|approvals_off|approvals_unknown|bad_intent|qty_cap|notional_cap|symbol_busy|sell_exceeds_position|no_position|market_closed|quote_stale|cap_unset`), `async place(ticket_id) -> dict` (outcome `awaiting_approval|placed|rejected|unknown`, ticket state), `Ticket` with `ticket_id`, `hash8`, `expires_at`, `state`, a safe `view()`; single-use, hashed over the intent plus ref_id; `halted()`, `halt_cause()`, `clear_halt()` (human-only, never exposed to the desk MCP).

- [ ] Failing tests for every Refused code above, single-use ticket, expiry, approvals flipping OFF between preview and place (re-read at commit point), transport error after send gives UNKNOWN + halt + no resend, `placed` reply when approvals ON expected `awaiting_approval` halts RH, audit lines carry no account number or free text, `.write` and `._http` pin rules respected.
- [ ] Implement; full suite; commit "feat(rh): Robinhood order guard core and mock write behaviour".

### Task 2: Approvals state machine and reconcile

**Files:** Modify `projectx_desk/rh/guard.py`, `projectx_desk/rh/state.py`; Test `tests/test_rh_approvals.py`.

**Interfaces:**
- Produces `Guard.track()` async loop body (called by the app's once-a-second deadline loop pattern): polls `approvals()` and orders every 15 s while any ticket is `awaiting_approval` (backoff to 60 s), moves tickets to `approved|declined|expired|filled`, frees symbol caps on `declined|expired`, counts `awaiting_approval` toward one-per-symbol and notional caps; `async reconcile(ticket_id)` looks the order up by `ref_id` (equity orders read) and resolves UNKNOWN to a state or leaves it UNKNOWN (never resends); `RhState.snapshot()` gains `tickets` (safe views) and `halt` (cause word or None).

- [ ] Failing tests for approve, decline, expire, reconcile found / not found, caps freeing, backoff, halt persisted across a restart (state file) and an UNKNOWN ticket surviving restart; commit "feat(rh): approval tracking and reconcile".

### Task 3: Routes

**Files:** Modify `projectx_desk/app.py`; Test `tests/test_rh_routes.py`, update `tests/test_px_app_actions.py` POST pin.

**Interfaces:** POST `/api/rh/preview`, `/api/rh/place`, `/api/rh/cancel`, `/api/rh/halt/resolve` (human-only, UI route, never an MCP tool), all through `body()` gate; GET `/api/rh/tickets`; fixed error codes mapped from `Refused`; routes not registered in `PX_RECORD_ONLY` or when `PX_RH=0`; the desk's own MCP (`projectx_desk/mcp.py`) gains no Robinhood tool, pinned by a test that lists its tools.

- [ ] Failing tests: gate (no origin/csrf = 403), record-only has no RH POST, MCP tool list unchanged, a chat prompt cannot reach any RH route, codes returned not detail; commit "feat(rh): order routes".

### Task 4: Panel, ticket UI, banner, screenshots

**Files:** Create `projectx_desk/static/rh.js`, `projectx_desk/static/rh.css`; one script/link tag in the HTML shell; Modify `scripts/screenshots.py` (Robinhood scene from the mock); Test `tests/test_rh_ui.py`.

**Interfaces:** panel with positions, option positions, working orders, pending approvals, tickets, a broker switch (ProjectX | Robinhood) in the header via the new file only, an order ticket (symbol, buy/sell, qty, limit) that previews, shows the hashed ticket and the plain-language warnings ("no paper trading, fill is unprotected, approve in the Robinhood app"), a 600 ms hold-to-place like the existing ticket, a DEMO banner when `PX_RH_DEMO` is set (state exposes a boolean `demo`), stream mode hides prices and P&L, everything rendered with `textContent`.

- [ ] Static pins (no innerHTML/eval, only `/api/rh/*` and `/api/csrf` fetched), node tests for pure formatting, screenshot scene from the mock only (no real account data); update `docs/ROBINHOOD.md` (phase 1 usage, caps env vars) and AGENTS.md rows; commit "feat(rh): Robinhood panel, ticket and demo banner"; push.

## Out of scope

Placement without approvals (phase 2), options orders, crypto, desk-owned stops, the canonical broker interface and ETL.
