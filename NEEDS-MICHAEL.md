# Needs Michael

Items an autonomous session could not settle within the rules. Each is safe to leave as is.

- **Live check.** The link was tested against a recorded-shape fixture with invented data and an offline dev desk, not against a
  running Public desk. A first run with `PX_PUB_URL=http://127.0.0.1:8701` (Public desk started with `PUBLIC_DESK_PORT=8701`) should show `Public link: on` at boot and `PUB: OK` in
  `/ai/digest`; `PUB: UNKNOWN code=bad_json` would mean the Public desk's agent-state version is no longer 2.
