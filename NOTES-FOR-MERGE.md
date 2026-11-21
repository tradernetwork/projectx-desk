# Notes for merge (delete this file after merging)

This branch does not touch `AGENTS.md` or `docs/DECISIONS.md` (another agent owned them). Add these by hand when merging.

## AGENTS.md: a Layout table row (next to `scripts/verify_audit.py`)

```
| `scripts/probe_report.py`, `docs/SIM_PROBE.md` | THE SIM PROBE (Round 36). `docs/SIM_PROBE.md` is a human-run runbook for one simulated account and 1 MES: steps S1 to S14 each name the unverified item they settle (Round 35's crossed stop, cancel on a partial fill, `fillVolume`/`customTag` on order rows, the inside-the-spread limit, CANCEL after a reload; Round 25's `PX_TRAIL_STOP` read-back, `PX_TRAIL_ENTRY`, one-lot; ROADMAP #1 logout halt, #2 one-lot trail probe, #4 Sunday-open throughput) with a pass or fail observation, a preconditions list, a do-not list and a stop-and-halt list. `scripts/probe_report.py` is standalone, stdlib only, read-only, NOT imported by the desk and wired into no route: it reads `data/audit.jsonl` (`--audit`) and an order-search reply saved with the desk stopped (`--orders`), binds S2L/S2S/S5/S11/S12 to one ticket with `--ticket STEP=HASH8` (matched, never printed), and prints PASS / FAIL / UNKNOWN per step with n beside every count. UNKNOWN when the evidence is absent, never an inferred pass; garbled lines skipped and counted; halt codes outside a fixed set print as `other`; no account id, balance, order id, ticket hash or tag in the output. Exit 0 table printed, 2 bad input. Pinned by `tests/test_probe_report.py` (no desk import, stdlib only, no `.write` attribute) |
```

## docs/DECISIONS.md: a new round

```
## Round 36: a sim probe to settle what is unverified, without order code (2026-11-20)

- **Why.** Rounds 25 and 35 and ROADMAP items 1, 2 and 4 each end in "needs a sim run", and LIMITATIONS still says no order write has
  ever been made on the real gateway. Nothing settles them but a human placing 1-lot orders on a simulated account, so the probe is a
  runbook and a report, not code.
- **What.** `docs/SIM_PROBE.md` (steps S1 to S14, phases A to E with the switches each one needs, the Sunday-open phase for the
  wide-spread limit and tick throughput, capture, the halt list, a by-hand sheet) and `scripts/probe_report.py` (read-only table over
  the audit and a saved order search). No change to `guard.py`, `app.py`, any default or the guard pin.
- **The report never infers a pass.** A step whose evidence is not in the files is UNKNOWN; by-hand items (S6 crossed-stop reply, S7
  partial-fill cancel, S13 trail modify and gap, S14 throughput) are always UNKNOWN in the table and live on the sheet. FAIL is
  reserved for an observed unsafe or contradicting outcome (an UNKNOWN write, a failed stop, a type-5 row whose `trailPrice` is a
  level rather than the distance the read-back assumes, a re-latched logout).
- **What the probe cannot settle.** A crossed post-fill stop cannot be produced by a click; a 1-lot cannot partially fill and the desk
  never cancels a filled entry; the desk never modifies a trail and flat-by forbids holding a gap; the TopstepX UI is not shown to be
  the same path as the API. These stay unverified after the probe and the runbook says so.
- **The one extra login.** `fillVolume`/`customTag` and the trail fields need raw order rows, which no desk route returns (they are
  allowlisted away on purpose). The runbook takes one `Order/search` with the desk STOPPED, never alongside it (Round 23).
```
