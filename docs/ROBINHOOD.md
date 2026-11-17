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
| `PX_RH` | `1` turns the feature on. Default `0`: nothing is built, no outbound traffic. |
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
Then set `PX_RH=1` and restart the desk.

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
- No POST route exists for Robinhood. `PX_RECORD_ONLY` registers none of this.

## Tool drift

```
python scripts/rh_capture.py --out tools-new.json
python scripts/rh_drift.py data/rh_tools.json tools-new.json     # exit 1: a tool the desk calls was removed or gained a required input
```

The capture holds tool names, descriptions and input-schema structure only, never account data.

---

Part of the Momentum Phinance toolchain. Built with [TraderMatrix](https://tradermatrix.pro?ref=MPHINANCE). The link carries a referral code; it costs you nothing.
