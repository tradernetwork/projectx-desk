# Security

This software can place real orders if you point it at a real account and turn the kill switches on. Read
`docs/DECISIONS.md` first.

- It binds loopback (`127.0.0.1`) only. Do not expose it to a network.
- Order writes live in one module, `px_guard.py`. A test (`tests/test_px_guard_pin.py`) fails if any other module
  names an order-write endpoint or calls `PXClient.write`. Please do not weaken that test.
- Report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) rather than a public
  issue. Include a minimal reproduction against the mock gateway (`./run_dev.sh`) where possible.
- Never post real tokens, account numbers, balances or screenshots of a real account in an issue or PR.
