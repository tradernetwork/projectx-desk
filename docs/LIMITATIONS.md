# Known limitations and what is unverified

Read this before you trust the desk with anything. It is the honest list, kept next to the code so it does not drift.

## Not verified against the real gateway

Everything is tested against the bundled mock gateway (`dev_gateway.py`). The mock follows the ProjectX documentation,
but it is not the real thing. Not yet confirmed on a real account:

- the whole order path (place, stop after fill, flatten, cancel, modify)
- `PositionType` Short = 2
- the `live` data-subscription flag on bars and contract search
- how your account's bracket mode is detected (it is never guessed: `PX_BRACKET_MODE` must be set explicitly)
- whether order search returns `customTag`, which reconcile-by-tag depends on
- `Position/closeContract` semantics, especially on live accounts
- whether your eval or funded account reports `simulated: true`. That decides whether cancel and stop-modify (documented
  as simulated-only) are available to you.
- the SignalR hubs on a real account: authentication, the exact event payloads, and what arrives when. The whole live feed,
  including the fill event that triggers the stop, rests on them.
- the gateway's rate limits (the client stays under the documented 50 per 30 s for bars and 200 per 60 s otherwise)

## Known limitations

**On loopback (the default) the HTTP API has no operator login.** `GET /api/csrf` is open and the `Origin` header can be set by any
non-browser client. Host, Origin and CSRF checks defeat web pages and DNS rebinding; they do not stop a local process. Anything
that can reach `127.0.0.1:<port>`, including an AI agent with shell or browser-automation access, can stage and place within the
caps. The MCP server cannot place, but an agent that also has a shell is not bounded by it. Run agents without access to the
desk's port, or accept that your caps are the real limit.

**Any other bind needs the operator token, and the token is the whole gate.** `PX_HOST` other than loopback refuses to start
without `PX_OPERATOR_TOKEN` (32+ characters) and `PX_ALLOWED_HOSTS`. The token is a login (HTTP Basic) for every route except
`/mcp/` and `/healthz`, and whoever has it can place orders within your caps. HTTP Basic is cleartext, so TLS must sit in front of
anything beyond a network you fully control (see [DEPLOY.md](DEPLOY.md)). There is no rate limit on login attempts beyond the
token's length. If you front a loopback-bound desk with a reverse proxy, set the token too: the refusal only triggers on the bind.

**P&L and drawdown are realised-only and static.** Daily P&L excludes unrealised P&L. `PX_DRAWDOWN_BUFFER` is a number you
type; it does not track your firm's trailing floor, your high-water mark, or a consistency rule. A large open loser is not
counted until it closes. Treat the budget as an upper bound that you keep updated by hand.

**A stop is sent after the first fill event, not with the entry** (in `position` bracket mode). If the user hub is down at
fill time, no stop is sent and no watchdog runs. In `oco` mode the bracket is atomic. The ticket says
"UNPROTECTED UNTIL STOP ACKED" while the stop is pending.

**Ambiguity is handled per write, not uniformly.** An ambiguous entry becomes `UNKNOWN`, writes a persistent halt file, and
blocks new opens until a human reconciles. An ambiguous or failed stop flattens and halts. `FLATTEN` halts and tells you to close in
the TopstepX UI if the close cannot be confirmed. Only with `PX_FLATTEN_FALLBACK=1` (default 0) does it send a second, different
closing order (an opposite-side market order capped at the net position read just before sending), and a late-executing first
close followed by that order could flip a position. Writes are never retried or re-sent byte for byte.

**Reconcile is manual.** `POST /api/reconcile` searches for the order by `customTag`; it never runs automatically and nothing
auto-clears a halt. Deleting `data/` clears the halt, the day's order count and the persisted intents.

**Live accounts are blocked by policy and by flags, not by a hard code wall.** A live account needs `PX_ACCOUNT_KIND=live`,
`PX_ALLOW_LIVE=1`, `PX_BRACKET_MODE=oco`, and market entries. It has never been run. Do not enable it.

**The browser cannot tell an AI extension's click from yours.** The 600 ms hold is anti-fat-finger friction. The server-side
caps are the control.

**Stream mode is client-side removal plus server redaction on the AI endpoints.** `/api/preview`, which only the UI calls,
returns dollar figures and the last digits of the account id regardless of stream mode, and "Reveal 30s" brings them back.
Prices, sizes and stop levels stay visible by design.

**Gateway text.** Contract names are reduced to an uppercase code or derived from the contract id; the gateway's description
is never forwarded; order tags are shown only as OWN or EXTERNAL; gateway error text is never repeated. Treat anything an
agent reads from this desk as data, not instructions.

**The AST pin is a tripwire, not a proof.** It catches the usual ways to name an order endpoint or call the write method
from another module, and `PXClient.post` independently refuses any path that is not a known read endpoint. Indirect or
dynamic construction can still evade a static check, so review new network code by hand.

**Platform.** The test suite passes on Python 3.10, 3.11 and 3.13 on Linux. macOS should work and is untested. On Windows use
WSL: `run_dev.sh` is a bash script that needs `curl`. `tzdata` is a requirement because the session clock uses
America/Chicago.

**`PX_MCP_PLACE` is a reserved flag.** No route sets an MCP origin on `place()`, so it does nothing today.

**The digest's GEX section is always `UNAVAILABLE`** until you wire a provider (see `docs/GEX_FUTURES.md`).

**Thresholds are hypotheses.** The ADX and ATR scalers, the behavioral-rail defaults and the level-reaction verdict margin
are starting points, not findings.

**The behavioral rails count closing fills, not round trips.** Cooldown, lockout and the size lock read today's fills that
carry a realised P&L. At a quantity cap of 1 that is the same thing; a 2-lot closed in two fills counts as two, and a scratch
(zero P&L) neither extends nor breaks a streak. Setting `PX_COOLDOWN_AFTER_LOSS_S` or `PX_MAX_CONSEC_LOSSES` to `0` turns that
rail off on purpose; a blank, garbled or negative value falls back to the default. `PX_SIZE_SCALERS=0` and
`PX_SIZE_AFTER_LOSS_LOCK=0` also switch their rails off.

**Quote age and drift.** The 2 second quote age applies to every open. The price-drift check (8 ticks since staging) applies to
market entries only; limit opens (simulated accounts only) skip it.

**A 401 or 429 on an order write becomes UNKNOWN and halts**, even though the order was certainly not processed. That is the
safe direction, and it means an expired token will halt the desk until a human clears it.

**Level reactions are a measurement with thresholds, not a finding.** "Held" means price moved at least 0.75 ATR away within 6
bars before closing 0.25 ATR through the level. The comparison is the same test on copies of that line shifted 0.5 to 1.5 ATR
away, which are not independent samples, and the verdict needs a 15 point gap and enough tests before it says anything positive.

**Stream mode is covered by tests on the data endpoints and the page's data, not on the rendered DOM.** The removal is real in
the code (`applySlots` swaps the figures out of the page) but no browser test pins it.

**The digest and the desk are open to anything on loopback.** The MCP server has a bearer token; `/ai/*`, `/state.json` and the
POST routes do not, for the reason given above.

## Known gaps

- `PX_NO_NEW_AFTER` only blocks new entries. Set `PX_FLAT_BY` to get the auto-flatten (Topstep requires flat by 3:10 PM CT); it is off until you set it, and it does not know holiday or early-close schedules.
- Drawdown room is the live balance minus `PX_MLL_FLOOR` if you set it, otherwise the number you type. The desk does not compute Topstep's end-of-day trailing floor or its lock; you update the floor. Unrealised loss counts only through each open trade's stop. The daily loss limit is your own number, not read from Topstep.
- Topstep objectives (profit target, 55% and 40% consistency, payout rules, the scaling plan) and conduct rules (news, hedging across accounts, prohibited strategies) are not enforced. See [TOPSTEP_RULES.md](TOPSTEP_RULES.md).
- `last_risk` (the size-after-loss lock) is replaced at commit time by a subsequent order even if the broker then rejects it.
- External trades (placed outside the desk) have no known risk cap.
- No calendar for holiday early closes.
- A missing day-state file resets the order count.
- `/state.json` and `/contracts` still forward some raw gateway fields (quote timestamps, contract id fallback names) that should be type-validated.
- A position held at restart that was placed by hand is reported (`EXTERNAL_UNPROTECTED`) but not protected.
- Scale-in is not supported: a second entry on a contract with a position, working order or entry in flight is refused.
