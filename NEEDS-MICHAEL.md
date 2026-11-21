# Needs Michael

Items an autonomous session could not settle within the rules. Each is safe to leave as is.

- **Round 36, the three advocates' verdicts.** `docs/DECISIONS.md` Round 36 has three placeholder lines ("to be recorded") for the
  advocates' one-sentence verdicts on the Public tab. The session that built the tab was told the decision and its conditions, not
  the verdicts, and did not invent them. Fill in one sentence each.
- **Port 8700.** The Public desk's default port (8700) is also `./run_dev.sh`'s default mock gateway port (`DEV_GATEWAY_PORT`). Running
  both needs one of them moved; `docs/PUBLIC.md` says so. Changing either default was left to you.
- **Live check.** The link was tested against a recorded-shape fixture with invented data and an offline dev desk, not against a
  running Public desk. A first run with `PX_PUB_URL=http://127.0.0.1:8700` should show `Public link: on` at boot and `PUB: OK` in
  `/ai/digest`; `PUB: UNKNOWN code=bad_json` would mean the Public desk's agent-state version is no longer 2.
