"""The Public tab (Round 36): a READ-ONLY loopback link to the separate Public.com sidecar desk.

`view.py` is pure (the allowlisted reduction of the sidecar's /ai/state JSON); `link.py` is the only I/O: one GET with a hard
timeout, a body cap and a short single-flight cache. Nothing in this package can send anything but a GET, and nothing in it is
read by the guard, sizing, limits or anchor (test pinned).
"""
