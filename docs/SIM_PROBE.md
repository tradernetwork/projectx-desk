# Sim probe: settling what the docs call unverified

A human-run runbook for ONE simulated ProjectX (TopstepX Combine or practice) account, one MES contract at a time. It writes no
code and changes no default. Every step names the item it settles, what to do, and what counts as pass or fail. The
evidence goes into files under `data/`, and `scripts/probe_report.py` turns them into a PASS / FAIL / UNKNOWN table keyed to the
step ids below. UNKNOWN means the evidence is not there; it is never read as a pass.

What it settles, and where each item comes from:

| Step | Settles | Source |
|---|---|---|
| S1 | clearing a latched `gateway_logout` halt, seeded `POS:`/`ORD:` before trusting the desk | ROADMAP Next #1, Round 23 |
| S2L, S2S, S2F | the first real order write: market entry, stop after the fill, FLATTEN; a short position reads `SHORT` (`PositionType` Short = 2) | LIMITATIONS "Not verified: any order write" |
| S3 | the CANCEL button pulls a resting limit (`Order/cancel` on sim) | Round 35 Decision 2 |
| S4 | the CANCEL button after a reload (absent) and the limit TTL as the fallback | Round 35, LIMITATIONS "The CANCEL button needs the page that staged the ticket" |
| S5a, S5 | the desk refuses a marketable limit; whether the SERVER refuses a limit priced inside a wide spread | Round 35 |
| S6a, S6 | the desk refuses a stop at or through the book; the gateway's reply to a crossed stop | Round 35 UNVERIFIED 1 |
| S7a, S7 | the desk refuses CANCEL after a fill; `Order/cancel` on a partially filled limit | Round 35 UNVERIFIED 2 |
| S8a, S8b | whether order rows carry `fillVolume` and `customTag` | Round 35 UNVERIFIED 3, LIMITATIONS (reconcile by `customTag`) |
| S9, S10 | `PX_TRAIL_STOP`: `arm_trail` places a type 5 with `stopPrice` None, reads back `trailPrice`/`trailDistance` | Round 25 "Still unverified", ROADMAP Next #2 |
| S11 | `PX_TRAIL_ENTRY`: the post-fill stop goes in as a trail | Round 25, AGENTS.md trail rule |
| S12 | `PX_ONE_LOT`: the 1-lot branch and its trail stop, from flat | Round 28, ROADMAP Next #2 (one-lot probe while flat) |
| S13 | modifying a live trail, a trail on a gap | ROADMAP Next #2 |
| S14 | tick throughput at the Sunday open (Sun 17:00 CT) | ROADMAP Next #4 |
| X0, X1 | no halt but the ones the runbook expects; no write went UNKNOWN | AGENTS.md "Never replay a write" |

## Do not

- **Never run this with a live account id.** `PX_ACCOUNT_KIND=sim`, `PX_ALLOW_LIVE=0`, and `PX_ACCOUNT_ID` is the simulated account.
  If `/state.json` shows `"account": {"simulated": false}` or anything you do not recognise, stop (see the halt list).
- **Never two sessions on one account.** One desk, one checkout, no second desk on another machine, no script that logs in to the
  API while the desk runs (a second login is what "Multiple sessions detected" means, Round 23). The only extra login in this runbook
  is the order-search capture, and it runs with the desk STOPPED.
- **Never paste balances, account ids, order ids or order tags in a chat**, an issue or a commit. `data/probe_orders.json` and
  `data/intents.json` carry them; keep them in `data/` (gitignored). The `probe_report.py` table carries counts and enums only and is
  safe to share. Keep the Ask box closed during the probe (it reads the live page, balances included).
- No `PX_MCP_PLACE`, no `PX_MCP_TRAIL`, no `PX_AGENT_TRAIL`, no `PX_FLATTEN_FALLBACK`, no `PX_FEED_STAGE`. Every write in the probe is
  a human click on the desk.
- Do not hold anything past `PX_FLAT_BY`, and do not hold a position over the 16:00 to 17:00 CT break or the weekend to "see the gap".
  Topstep requires flat by 15:10 CT (`docs/TOPSTEP_RULES.md`).
- Do not script the clicks or loop them. A handful of 1-lot orders over a session; Topstep's prohibited strategies include rapid
  trades on simulated fills (`docs/TOPSTEP_RULES.md`).
- Do not edit or delete `data/audit.jsonl` during the probe. It is the evidence.

## Preconditions (tick each one)

- [ ] Flat: the TopstepX UI shows no position and no working order on the account, MES or anything else.
- [ ] The halt file `data/HALT` is absent, OR it holds `gateway_logout` and S1 is the first step you run.
- [ ] `data/intents.json` has no intent in state SENDING or UNKNOWN (if it does, reconcile and resolve those first; `/state.json`
      `unknown_order` must be empty).
- [ ] The day's order count leaves room: the probe places up to 8 entries plus their stops (`PX_MAX_ORDERS_DAY`, default 10). Split
      the phases over two trading days if needed.
- [ ] Inside the session window, and at least 30 minutes before `PX_FLAT_BY` and `PX_NO_NEW_AFTER`.
- [ ] Audit baseline saved: `.venv/bin/python scripts/verify_audit.py --save` (so the probe's audit can later be shown to have only grown).
- [ ] A TopstepX Personal Daily Loss Limit set as a platform-side brake (`docs/TOPSTEP_RULES.md`).

Environment for every phase (`.env.projectx` or the shell; a change needs a desk restart, and a restart only while flat):

```
PX_ACCOUNT_KIND=sim
PX_ALLOW_LIVE=0
PX_ACCOUNT_ID=<the simulated account>
PX_BRACKET_MODE=position        # S2 to S12 exercise the stop-after-fill path
PX_TRADING=1                    # sim only, for the duration of the probe; back to 0 afterwards
PX_MCP_PLACE=0
PX_MAX_QTY_ORDER=1
PX_ALLOWED_ROOTS=MES
PX_FLAT_BY=15:08
PX_NO_NEW_AFTER=14:55
PX_LIMIT_TTL_S=120              # S4 waits for it; the default 300 also works, slower
PX_FLATTEN_FALLBACK=0
PX_MCP_TRAIL=0
```

Per phase, set ONLY these and leave the rest off:

| Phase | Steps | Switches |
|---|---|---|
| A | S1 to S8 | `PX_TRAIL_STOP=0`, `PX_TRAIL_ENTRY=0`, `PX_ONE_LOT=0` |
| B | S9, S10 | `PX_TRAIL_STOP=1` |
| C | S11 | `PX_TRAIL_ENTRY=1` (and `PX_TRAIL_STOP=0`) |
| D | S12 | `PX_ONE_LOT=1` (and `PX_TRAIL_ENTRY=0`: the 1-lot branch uses the trail path without it) |
| E | S5, S6a, S14 at the Sunday open | phase A switches, `PX_TICK_BARS=500,1000` |

Write down, for every entry you place, the 8-character id on the staged ticket (the same 8 characters as `ticket` on its
`preview` line in `data/audit.jsonl`). S5, S11 and S12 are bound to one ticket by it.

## Steps

Every step ends flat. Read `/state.json` (`halted`, `halt_cause`, `positions`, `orders`) and the digest (`/ai/digest`) when a step
says so.

### S1: clear a latched `gateway_logout` halt (ROADMAP Next #1)
1. Confirm flat in the TopstepX UI. Start the desk (`python -m projectx_desk`).
2. `/state.json`: `halted: true`, `halt_cause: "gateway_logout"`, `seeded.positions: true`.
3. `/ai/digest`: `POS:` and `ORD:` print `NONE`, never `UNKNOWN`. `UNKNOWN` means the desk has not re-read the account: wait one
   `PX_RESEED_S` (60 s) and read it again. Do not clear while it says `UNKNOWN`.
4. Click "Clear halt (human only)". Then watch the session: no new `gateway_logout`.
- **Pass:** audit `halt code=cleared` after the last `halt code=gateway_logout`, and none after it.
- **Fail:** a second `gateway_logout` in the session (something else holds the login: stop, find it).
- No latched halt at the start: S1 stays UNKNOWN. That is fine; note it on the sheet.

### S2L, S2S, S2F: the first order write (phase A)
1. Stage a manual BUY of 1 MES at market with the default stop. Check the readback and the 8-character id. Hold to place.
2. Watch the bracket tree: entry PLACED, the fill, the stop SENDING then ACKED. The ticket reads "UNPROTECTED UNTIL STOP ACKED"
   only until the ACK. Confirm a working SELL STOP of size 1 in the TopstepX UI.
3. FLATTEN. The TopstepX UI shows no position and no working order.
4. Repeat with a SELL. Before FLATTEN, `/state.json` `positions[0].side` must read `SHORT` (it reads `null` if the gateway's
   short is not type 2): write that down for `PositionType` Short = 2.
- **Pass (S2L, S2S):** `place`, `ack` and `stop state=ACKED` for the ticket. **Pass (S2F):** `flatten state=FLAT`.
- **Fail:** `unknown`, a broker `reject`, `stop state=FAILED` (the desk flattens and halts `naked_position` by itself), `flatten
  state=NOT_FLAT`.

### S3: the CANCEL button (phase A)
1. Stage a BUY LIMIT 1 MES 8 ticks under the bid (inside the guard's 20-tick `entry_deviation` limit, `max_entry_dev_ticks` in `limits.py`). Place. It rests.
2. Click CANCEL on the bracket tree. `entry_status` reads `cancelled`; the TopstepX UI shows no working order.
- **Pass:** audit `cancel code=user state=OK`. **Fail:** `state=UNKNOWN` (and halt `cancel_unknown`), or `state=FAILED` with no OK.
- If the limit filled before the click, the cancel is refused (`has_fill`) and the stop arms: that is S7a, flatten and redo S3.

### S4: CANCEL after a reload, and the TTL (phase A)
1. Stage and place another resting BUY LIMIT, as in S3. Reload the page.
2. The CANCEL button is NOT shown (the page lost the ticket id). Write that down.
3. Wait `PX_LIMIT_TTL_S` (120 s). The order disappears from the TopstepX UI.
- **Pass:** audit `cancel code=limit_ttl state=OK`. **Fail:** UNKNOWN or FAILED as in S3. The "button absent" half is by hand.

### S5a, S6a: the desk's own refusals (phase A, or E)
- **S5a:** stage a BUY LIMIT at or above the ask. Expected: refused, audit `reject check=limit_rests`. If the page itself refuses
  to stage a marketable limit, no preview reaches the server and S5a stays UNKNOWN; the server side is pinned by
  `tests/test_px_guard_book.py`.
- **S6a:** stage a market BUY with a manual stop AT the bid. Expected: refused, audit `reject check=stop_clear_of_market`.
- Nothing is sent in either step.

### S5: a limit inside a wide spread (phase E, Sunday open)
1. When MES quotes a spread of 3 ticks or more (most likely right after the 17:00 CT Sunday open; unverified how often), stage a
   BUY LIMIT one tick above the bid and still under the ask. The desk lets it through (`limit_rests`: strictly under the ask).
2. Place. Note the 8-character id. If it rests, CANCEL it (or let the TTL); if it fills, FLATTEN.
- **Report:** `--ticket S5=<id>`. PASS either way, with the answer: ACCEPTED (an `ack`) or REFUSED (a broker `reject` and its
  errorCode). Fail only if the write went UNKNOWN.

### S6: the gateway's reply to a crossed stop (by hand; not settled by this probe)
The desk refuses a crossed stop before the entry (S6a). The only remaining case is a fill after which the market has already run
through the planned stop, which cannot be produced on purpose by a human click. If it happens in a fast market, the audit shows
`stop state=FAILED` for that ticket and the desk flattens and halts (`naked_position`). Then save the order search (below) and look
for that ticket's rejected type-4 row; write down whether it was rejected or filled at once. Placing a crossed stop from the
TopstepX UI does not settle it: the UI is not this API (unverified whether it uses the same path).

### S7: cancel on a partial fill (by hand; not reachable at qty 1)
A 1-lot cannot partially fill, and the desk never sends a cancel for an entry with any fill (`has_fill`), so the desk does not
depend on the answer. S7a passes only if a CANCEL click races a fill (audit `cancel state=REFUSED code=has_fill`); it is pinned by
`tests/test_px_guard_cancel.py` and is expected to stay UNKNOWN here. S7 stays UNKNOWN.

### S9: `arm_trail` (phase B, `PX_TRAIL_STOP=1`)
1. Open a market BUY 1 MES as in S2 and wait for the fixed stop ACKED.
2. In the manage panel, Trail with a distance SMALLER than the current stop distance (it must start tighter), for example 4.5 points.
3. The panel says "Trailing stop armed". The TopstepX UI shows ONE working order on the contract: a TRAIL, size 1, SELL. The fixed
   stop is gone.
4. FLATTEN. The sweep cancels the trail too (Round 24): no working order left.
- **Pass:** audit `trail state=ARMED`. **Fail:** `REJECTED` (the gateway refused a type 5 with `stopPrice` None: the old stop stays,
  no halt; that IS the answer to Round 25's open item), `MISREAD`, `UNSEEN`, `UNKNOWN` (and halts `trail_*`).
- `CONTEXT_CHANGED` or `FLAT_AFTER` mean the position moved under the swap: UNKNOWN, rerun.
- **Two working stops on one position at any moment: FLATTEN and HALT now** (a reverse position when both fill).

### S10: what order search says about a trail (after phase B)
From the saved order search (below): every type-5 row carries `trailPrice` equal to `trailDistance` x 0.25 (the DISTANCE, as the
read-back in `guard.py` `_trail_misread` assumes) and a finite `stopPrice` (what `state.is_protective_stop` needs for a trail).
- **Pass:** all type-5 rows consistent. **Fail:** a row whose `trailPrice` is a price level, or a missing field: the read-back
  assumption is wrong and `PX_TRAIL_STOP` must stay off until `guard.py` is corrected.

### S11: trail as the entry stop (phase C, `PX_TRAIL_ENTRY=1`)
1. Market BUY 1 MES. Note the id. After the fill, the TopstepX UI shows a TRAIL (type 5), not a STOP.
2. FLATTEN.
- **Report:** `--ticket S11=<id>`. **Pass:** `stop state=ACKED` with no fallback. **Fail:** `TRAIL_FALLBACK_FIXED` (the contract
  record or distance was unreadable and a fixed stop went in), `TRAIL_MISREAD` or `FAILED` (the desk flattens and halts).

### S12: one-lot mode from flat (phase D, `PX_ONE_LOT=1`; ROADMAP Next #2)
1. Flat. Manual BUY, qty 1, MES, the ATR stop in the box. The readback shows the 1-lot branch (`MODE: ... 1LOT` in the digest).
2. Place. After the fill the stop is a TRAIL. Write down the trail distance the TopstepX UI shows against the stop distance on
   the ticket (they should match, Round 28).
3. FLATTEN.
- **Report:** `--ticket S12=<id>`. **Pass:** `preview one_lot=1` and `stop state=ACKED` with no fallback. **Fail:** no `one_lot=1`
  (the ticket did not take the branch), or a fallback, misread or failed stop.

### S13: modifying a live trail, a trail on a gap (by hand; not settled by this probe)
The desk never modifies a trail (`move_stop` refuses `trail_stop`) and has no code that sends a modify to one, so the API question
cannot be asked from the desk. Editing a trail in the TopstepX UI tells you what the platform allows, not what `Order/modify`
does (unverified that they share a path). A gap cannot be probed within the flat-by rule. If a resting trail meets a fast move
during S9, S11 or S12, write down where it filled against its trigger.

### S14: tick throughput at the Sunday open (phase E, Sun 17:00 CT)
1. Desk up from 16:50 CT with `PX_TICK_BARS=500,1000`, MES on screen, a 500-tick chart open. Flat; this step places nothing.
2. For the first 15 minutes: every minute count the 500-tick bars that closed (`/api/tickbars?contract=<id>&n=500`, the bar
   times), so prints per minute is about 500 x bars. Note CPU of the desk process (`ps -o %cpu -p <pid>` or `top`).
3. Lag: compare `quote.timestamp` with `asof` in `/state.json?contract=<id>`. `asof` has whole seconds only, so this is a coarse
   gateway-to-desk figure; the hub-to-browser lag has no instrument in the desk (unverified method; a browser timing would be new
   code).
- Sheet only; the report prints UNKNOWN for it by design.

## Capture

After the last step, desk still running:

1. `.venv/bin/python scripts/verify_audit.py` should say `grew`. Anything else (`edited`, `shrunk`) means the evidence was touched.
2. Copy into `data/probe-<date>/` (gitignored): `data/audit.jsonl`, `data/HALT` if present (one fixed word), `data/intents.json`,
   and for S14 `data/ticks.json` and `data/flow.db`. Keep the desk's console log too.

Then STOP the desk (flat), and only then take the order search, the one extra login of the probe:

```
set -a; . ./.env.projectx; set +a
.venv/bin/python - <<'EOF'
import json, os, urllib.request
api = os.environ.get("PROJECTX_API", "https://api.topstepx.com")
def post(path, body, token=None):
    h = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
    with urllib.request.urlopen(urllib.request.Request(api + path, json.dumps(body).encode(), h)) as r:
        return json.load(r)
tok = post("/api/Auth/loginKey", {"userName": os.environ["PROJECTX_USERNAME"], "apiKey": os.environ["PROJECTX_API_KEY"]})["token"]
out = post("/api/Order/search", {"accountId": int(os.environ["PX_ACCOUNT_ID"]),
           "startTimestamp": "<probe start, UTC ISO>", "endTimestamp": "<probe end, UTC ISO>"}, tok)
with open("data/probe_orders.json", "w", encoding="utf-8") as f:
    json.dump(out, f)
print("orders:", len(out.get("orders") or []))
EOF
```

The endpoint and body are the ones `projectx_desk/client.py` uses (`/api/Auth/loginKey`, `/api/Order/search`). Whether this extra
login, with the desk stopped, affects the next desk session is unverified: after restarting the desk, check `/state.json` for a
fresh `gateway_logout` before trusting it.

Then the table:

```
.venv/bin/python scripts/probe_report.py --audit data/audit.jsonl --orders data/probe_orders.json \
    --ticket S5=<id> --ticket S11=<id> --ticket S12=<id>
```

Optional bindings for S2L and S2S pin those rows to one ticket each. Exit 0 means the table printed; exit 2 means a file was
missing or not what the report expects.

## Stop, FLATTEN and HALT if

- Any halt banner other than the one you clicked (`manual`) or the latched `gateway_logout` of S1. Do not clear it: write down
  `halt_cause`, confirm flat in the TopstepX UI (close there if the desk could not), save the files, run the report.
- A position with no working stop for more than about 10 seconds after the fill (the watchdog should already have flattened it).
- Two working stops, or a stop and a trail, on one position.
- `POS:` or `ORD:` print `UNKNOWN` while you think you are flat, or the TopstepX UI and `/state.json` disagree.
- `/state.json` `account.simulated` is not `true`, or the account is not the one you set.
- A `gateway_logout` during the probe.
- An audit line with `unknown`, `stop state=FAILED`, `trail state=UNSEEN|UNKNOWN|MISREAD`, or `cancel state=UNKNOWN`.
- A fill on a limit you cancelled whose stop does not arm (Round 35, "Not fixed").

Afterwards: `PX_TRADING=0`, and every switch from phases B to D back to 0.

## Sheet (by hand)

| Item | Observation |
|---|---|
| S1 `POS:`/`ORD:` before clearing | |
| S2S `positions[0].side` | |
| S4 CANCEL button after reload | |
| S5 spread at the time (ticks) | |
| S6 crossed-stop reply, if it happened | |
| S12 trail distance vs ticket stop distance | |
| S13 trail fill vs trigger, if it happened | |
| S14 prints/min, coarse lag, CPU, minute by minute | |

## Not grounded in the repo (unverified)

- How often MES quotes a 3-tick or wider spread at the Sunday open (S5 depends on it).
- Whether the TopstepX web UI uses the same order path as the API (S6, S13).
- Whether a REST login with the desk stopped affects the next desk session (the capture step).
- The S14 lag method is coarse (whole seconds); the desk has no hub-to-browser timing.
- Whether a stop write after a fill can be made to cross on purpose: it cannot be by a human click, so S6 is opportunistic.
