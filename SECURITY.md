# Security

This software can place real orders if you point it at a real account and turn the kill switches on. Read
`docs/DECISIONS.md` first.

- It binds loopback (`127.0.0.1`) only. Do not expose it to a network.
- Order writes live in one module, `px_guard.py`. A test (`tests/test_px_guard_pin.py`) is a tripwire that fails when another
  module names an order-write endpoint, reaches the raw HTTP client or bearer token, or calls the write method. It is not a
  proof: indirect or dynamic construction can evade a static check, and `PXClient.post` independently refuses any path that is
  not a known read endpoint. Review new network code by hand, and please do not weaken the test.
- The local HTTP API has no operator login. Anything that can reach the port can stage and place within your caps. Run
  untrusted agents without access to it (see docs/LIMITATIONS.md).
- Report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) rather than a public
  issue. Include a minimal reproduction against the mock gateway (`./run_dev.sh`) where possible.
- Never post real tokens, account numbers, balances or screenshots of a real account in an issue or PR.
