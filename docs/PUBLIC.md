# The Public tab (read-only link to a separate Public.com desk)

The futures desk can SHOW what a separate Public.com desk process reports. It cannot do anything else with it. Orders, cancels,
confirms, wheel staging, ledger records and halt clearing happen in the Public desk, in that desk's own page, and nowhere here.
Decision record: `docs/DECISIONS.md` Round 36.

## Turn it on

1. Run the Public desk on the same machine. It listens on `127.0.0.1:8700` by default. Note that `./run_dev.sh` also uses port
   8700 for the mock ProjectX gateway: when you run both, move one. Recommended: start the Public desk with `PUBLIC_DESK_PORT=8701` and set `PX_PUB_URL=http://127.0.0.1:8701`
   (the mock gateway keeps 8700, overridable with `DEV_GATEWAY_PORT`).
2. Set `PX_PUB_URL` for the futures desk, for example `PX_PUB_URL=http://127.0.0.1:8700`, and restart it.

The boot log prints `Public link: on`, or `Public link off: not_allowed` when the value is not a plain loopback http URL. It never
prints the URL itself.

## What you get

- A **Public** button on the broker switch at the bottom left (next to ProjectX and Robinhood, or a ProjectX | Public switch of
  its own when Robinhood is off). It opens a read-only panel: halt banner, account type and permissions, balances, positions,
  working orders, the Public desk's tickets, wheel cycles with their true basis, wash sale flags, the data age, and UNKNOWN wherever
  the Public desk could not read something. The panel carries a permanent note: "Display only. Orders happen in the Public desk,
  not here."
- `GET /api/pub/state` (`?stream=1` drops every dollar field): the allowlisted JSON the panel draws.
- `/state.json` gains `pub: {enabled, reachable, code, age_s}` from the last read (it never triggers one).
- `/ai/digest` gains `PUB`, `PUB_HALT`, `PUB_POS`, `PUB_ORD`, `PUB_TKT`, `PUB_WHEEL` and `PUB_WASH` lines (at most 8 items and 400
  characters a line). A Public desk that cannot be read prints `PUB: UNKNOWN code=<code>`, never OK and never nothing. The prefixes
  never collide with the futures `POS:` and `ORD:` lines, and `/llms.txt` tells an agent never to add them to the futures numbers.

## The rules (each one test pinned)

- `PX_PUB_URL` unset: no route, no tab, no digest line, no `pub` block. Under `PX_RECORD_ONLY=1`: no link, no route, no fetch,
  whatever `PX_PUB_URL` says.
- The URL must be `http://` to `127.0.0.0/8`, `::1` or `localhost`, with no user name or password, query, fragment or path. Anything
  else is `not_allowed` and nothing is fetched.
- One request shape only: `GET /ai/state?stream=0|1` with `Accept-Encoding: identity` (a compressed reply is refused). No
  redirect is followed, proxy variables are ignored, the body is capped at 256 KB, and the fetch plus the parse (in a worker
  thread, every number bounded before it is formatted) is bounded by 3 seconds, so a hung or hostile Public desk cannot hold the
  deadline loop. One result per stream flag, single flight: a success is fresh for 5 seconds and a failure for 10, counted from
  the end of the attempt; a failure is never covered with older data.
- The digest, `/ai/state` and the MCP state never wait on the Public desk: they show the last result with its age while a refresh
  runs in the background (`PUB: UNKNOWN code=pending` until the first attempt ends). Only the panel's own route waits, at most 3 s.
- Error codes are fixed words: `unset unreachable timeout bad_status too_big bad_json not_allowed pending`. No text from the
  Public desk is ever shown: the halt cause, the poll error and the ledger code are closed sets copied from the Public desk's own
  code, and anything else reads `unreadable`.
- The reduction (`projectx_desk/pub/view.py`) is an allowlist: enums from fixed sets, validated symbols and OSI option symbols,
  dates, and numbers as decimal strings (the JSON is parsed with `Decimal`, so no float exists). No account number, ticket id,
  order reference, confirm hash, proposal id or ledger event id crosses. A section the Public desk could not read stays UNKNOWN,
  never an empty list. A ROW this desk cannot read (an OCC-padded option symbol, `BRK/B`) is counted, never dropped silently: the
  digest prints `PUB_POS: n=0 unreadable=1`, the panel says INCOMPLETE, and a list cut at 200 rows prints `n=200+`. NONE means the
  Public desk sent an empty list and every row was read.
- No futures path reads it: `guard.py`, `sizing.py`, `limits.py` and `anchor.py` import nothing from `projectx_desk/pub/`, the
  phone-alert notifier builds its state without it, and Public numbers enter no Topstep calculation and no cross-broker sum.
- Nothing is shared with the Topstep path: no file, halt, lock, audit line or thread. The link writes nothing to disk.
- The pin (`tests/test_px_guard_pin.py`) bans, outside the two guards, any import or mention of the Public desk's package or of
  Public's SDK, its writer machinery and write method names, and the Public desk's write routes as string literals; `pub/` may
  only send GET.
- There is no MCP tool, no chat path and no POST route for it.

## Not built (phase 1, deferred)

Showing or staging Public tickets from this desk is deferred until a live, read-only run against a real Public account has been
recorded. Until then the only thing this desk does with the Public desk is read its agent state.
