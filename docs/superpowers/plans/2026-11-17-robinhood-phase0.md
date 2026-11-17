# Robinhood Phase 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** a read-only Robinhood layer that anyone can run offline against a mock MCP server, with no order path yet.

**Architecture:** new package `projectx_desk/rh/` (one module per job, pure where possible), a mock server `projectx_desk/dev_robinhood.py`, read-only routes and digest lines, capture and drift scripts. ProjectX code (`guard.py`, sizing, limits) is not modified.

**Tech Stack:** Python 3.10+, FastAPI, httpx, `cryptography` is NOT needed. Tests use `httpx.MockTransport` or the mock server in-process.

**Spec:** `docs/superpowers/specs/2026-11-17-robinhood-broker-design.md`. Reference code (Rust, read for flow and payload shapes only): `tradernetwork/tradermatrix-desktop` `apps/desktop/src-tauri/src/rh/{mcp,oauth,tokens,limiter,reads,types}.rs`, fixtures `apps/desktop/src-tauri/tests/fixtures/rh/`, `scripts/rh-tool-drift.py`; fetch with `gh api repos/tradernetwork/tradermatrix-desktop/contents/<path> -H 'Accept: application/vnd.github.raw'`.

## Global Constraints

- Only `projectx_desk/guard.py` (and, from phase 1, `projectx_desk/rh/guard.py`) may name an order-write endpoint or write tool. Phase 0 ships NO write tool call anywhere. `tests/test_px_guard_pin.py` is extended, never narrowed.
- No bare `.write(` attribute access outside guard modules (AST pin). Write files with `print(..., file=f)`, `Path.write_text`, `json.dump`, then `os.replace`.
- No em dashes or en dashes in code, comments, docs or copy.
- `PX_RH` (default 0) gates the whole feature in a real install; `PX_RH_TRADING` default 0 and unused in phase 0. `run_dev.sh` sets `PX_RH=1` against the mock.
- `PX_RECORD_ONLY` registers zero Robinhood POST routes (phase 0 has none anyway). AI surface is an allowlist: numbers, enums, validated symbols; no account number, balance, free text. Stream mode hides Robinhood money values.
- MCP endpoint `https://agent.robinhood.com/mcp/trading`; https only (loopback http allowed for the mock); redirects never followed; 4 calls/s limiter; reads retry once; token files mode 0600, atomic, rotating refresh token saved BEFORE the old one is spent.
- Every Robinhood account except the dedicated MCP account (the one with the agentic-allowed flag) is dropped before reaching state, digest or Ask agent.
- Do not touch `static/desk.js` (owner has uncommitted edits) unless a task says so. Run `. .venv/bin/activate && pytest -q` before each commit; all must pass. Commit with trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- Mock returns an account list containing a NON-MCP account: it must never appear in state, digest or any route. (Task 3)
- Token refresh response arrives but the file write fails: the old token must not be spent twice and the failure reads as read-down, not a crash. (Task 2)
- A tool result with missing, null or non-finite numbers: views drop the field (UNKNOWN), never default to 0. (Task 3)
- Server returns a redirect, an oversized body or invalid JSON-RPC: fixed error code, never provider text. (Task 1)
- Weekend or `PX_RH=0`: no outbound traffic at all. (Task 3)

---

### Task 1: Mock server and MCP client

**Files:** Create `projectx_desk/dev_robinhood.py`, `projectx_desk/rh/__init__.py`, `projectx_desk/rh/mcp.py`; Test `tests/test_rh_mcp.py`.

**Interfaces:**
- Produces `dev_robinhood.make_app(accounts=None) -> ASGI app` serving Streamable-HTTP JSON-RPC at `POST /mcp` (`initialize`, `tools/list`, `tools/call`) for the read tools get_accounts, get_portfolio, get_equity_positions, get_equity_orders, get_equity_quotes, get_option_positions, get_option_orders, get_option_chains, get_realized_pnl, get_trade_approvals, get_trade_approval_setting, plus stub OAuth endpoints (`/.well-known/oauth-authorization-server`, `/register`, `/authorize` redirecting to the callback with a code, `/token` rotating refresh tokens single-use). Default data: one MCP account (agentic allowed), one personal account (not allowed), a few equity and option positions, one pending approval. Write tools are listed by `tools/list` but `tools/call` on them returns a fixed `write_disabled_in_mock` error.
- Produces `rh.mcp.Client(base_url, token_provider, transport=None)` with `async call(tool: str, args: dict) -> dict` and `async list_tools() -> list[dict]`; raises `rh.mcp.McpError(code)` with fixed codes `unauthorized|rate_limited|unreachable|timeout|bad_reply|too_big|redirect|write_blocked`. Client refuses any tool name outside `READ_TOOLS` (a frozenset) with `write_blocked`.

- [ ] Write failing tests: initialize+list+call round trip against the mock via `httpx.ASGITransport`; each error code; `place_equity_order` and `cancel_equity_order` raise `write_blocked` before any request is sent; 4/s limiter (injected clock); one read retry on 401 only after token_provider refresh; redirect refused; body over 1 MB `too_big`.
- [ ] Run, see them fail. Implement the mock, then the client. Run, see them pass.
- [ ] Full `pytest -q`, commit "feat(rh): mock Robinhood MCP server and read-only MCP client".

### Task 2: OAuth and token store

**Files:** Create `projectx_desk/rh/oauth.py`, `projectx_desk/rh/tokens.py`; Test `tests/test_rh_oauth.py`.

**Interfaces:**
- Produces `tokens.TokenStore(path)` with `load() -> Tokens | None`, `save(tokens) -> None` (atomic, 0600), `Tokens` dataclass (access, refresh, expires_at, client_id; repr hides secrets); `async access_token() -> str` refreshes when within 60 s of expiry, saves the NEW refresh token before using the new access token, raises `McpError("unauthorized")` when refresh fails or the file is missing/garbled.
- Produces `oauth.Login(base_url, store, callback_port=None, opener=None, transport=None)` with `async start() -> {url, pending_id}` and `status(pending_id) -> pending|done|expired|failed`: dynamic client registration, PKCE S256, state check, loopback listener on `127.0.0.1` (ephemeral, or `PX_RH_CALLBACK_PORT`), callback host `localhost`, 10 min expiry, one pending login at a time, opener only called with https URLs on the configured auth host. The listener must not use a `.write` attribute (use `writer.writelines`).

- [ ] Failing tests against the mock: full login end to end; wrong `state` fails; refresh rotates and the old token is dead; save failure keeps the old file and reports read-down; file mode 0600; repr and logs never contain tokens; second concurrent login `rate_limited`.
- [ ] Implement; tests pass; full suite; commit "feat(rh): OAuth PKCE login and rotating token store".

### Task 3: Reads, views, state and digest

**Files:** Create `projectx_desk/rh/reads.py`, `projectx_desk/rh/views.py`, `projectx_desk/rh/state.py`; Modify `projectx_desk/digest.py` (add `RH:` lines and `rh` key), `projectx_desk/app.py` (read-only routes, startup poll, `PX_RH` gate); Test `tests/test_rh_reads.py`, `tests/test_rh_app.py`.

**Interfaces:**
- Produces `views.position_view(raw) -> dict|None`, `order_view(raw)`, `approval_view(raw)`, `account_view(raw)`: allowlisted fields only, canonical-shaped (`symbol`, `asset` equity|option, `qty`, `avg_price`, `side`, `status`, option `expiry`/`strike`/`right`), non-finite or missing numbers are omitted and listed as unknown, never 0. No account number or balance in any view.
- Produces `reads.Reader(client)` with `async accounts()`, `positions()`, `orders()`, `option_positions()`, `approvals()`, `approval_setting()`, `realized_pnl()`: every call first resolves the MCP account (agentic-allowed flag) and drops every other account; zero or several MCP accounts reads as UNKNOWN, not a guess.
- Produces `state.RhState` (in-memory snapshot with `as_of`, `ok`, `unknown`) refreshed by a background task every `PX_RH_POLL_S` (default 30, floor 10); stands down when `tradeday.market_quiet()` and flat, and entirely when `PX_RH=0`.
- Routes (GET only): `/api/rh/state`, `/api/rh/positions`, `/api/rh/orders`, `/api/rh/approvals`; `PX_RH=0` returns `{"enabled": false}` and makes no outbound call. Digest `RH:` lines carry counts, symbols, qty and approval counts; dollar values appear only when not in stream mode.

- [ ] Failing tests: non-MCP account never appears anywhere (state JSON, digest text, every route, repr); missing/NaN numbers omitted; zero or two MCP accounts reads UNKNOWN; `PX_RH=0` makes no request (assert transport never called); stream mode hides dollars; `PX_RECORD_ONLY` has no RH routes; digest length cap holds.
- [ ] Implement; tests pass; full suite; commit "feat(rh): read-only Robinhood state, views, digest and routes".

### Task 4: Capture and drift scripts, dev script, docs

**Files:** Create `scripts/rh_capture.py`, `scripts/rh_drift.py`, `docs/ROBINHOOD.md`; Modify `run_dev.sh`, `run_dev.ps1`, `AGENTS.md` (layout rows), `docs/DECISIONS.md` (Round 26), `tests/test_px_guard_pin.py` (extended: second allowed writer `projectx_desk/rh/guard.py`, and every Robinhood write tool name pinned as a forbidden string outside guard modules); Test `tests/test_rh_scripts.py`.

**Interfaces:**
- `rh_capture.py` logs in via the token store, saves `tools/list` (names, descriptions, input schemas, no account data) to a JSON file; `rh_drift.py old.json new.json` exits 1 when a tool the desk calls was removed or gained a required input (port of `scripts/rh-tool-drift.py`).
- `run_dev.sh` / `run_dev.ps1` start the mock on a free loopback port and set `PX_RH=1`, `PX_RH_URL`, a DEMO banner flag. UI banner and Robinhood panel are phase 1; phase 0 only exposes the routes and digest.

- [ ] Failing tests: drift exit codes (removed tool, new required input, no change), capture output contains no account fields, pin test fails if a string `place_equity_order` appears in a non-guard module.
- [ ] Implement; docs written (risk statement: no paper trading, approvals must be ON before phase 1, dedicated small account; login on localhost or via `ssh -L` for a headless host; `?ref=MPHINANCE` TraderMatrix link in the footer only); `pytest -q`; commit "feat(rh): capture and drift scripts, dev mock wiring, docs"; push.

## Out of scope (phase 1 plan, written after phase 0 lands)

Order ticket, `rh/guard.py`, approvals polling state machine, Robinhood panel and broker switch in the UI, DEMO banner, stream-mode UI hiding, screenshots scene.
