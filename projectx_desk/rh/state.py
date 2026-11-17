"""In-memory Robinhood snapshot and its poller. Read-only: nothing here can reach an order path.

`Runtime` owns a `Reader` and an `RhState`. `poll_once` makes NO call when the token file is missing (login is a later
task) or when the market is quiet (Fri 16:05 CT to Sun 16:55 CT) and the last snapshot shows no working order and no
pending approval. A section that could not be read is None (UNKNOWN), never an empty list. A failed read leaves only a
fixed error code. `PX_RH=0` (the default) means `from_env` returns None and nothing is built.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

from .. import ROOT
from .. import tradeday as px_tradeday
from .mcp import ENDPOINT, Client, McpError
from .reads import Reader, Unknown
from .tokens import TokenStore

DEFAULT_POLL_S = 30.0
MIN_POLL_S = 10.0
WORKING = {"queued", "unconfirmed", "confirmed", "partially_filled", "pending"}
DOLLAR_KEYS = ("avg_price", "price")
SECTIONS = ("account", "positions", "orders", "approvals", "approval_setting", "pnl")


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def poll_interval(env) -> float:
    try:
        v = float(env.get("PX_RH_POLL_S") or DEFAULT_POLL_S)
    except (TypeError, ValueError):
        return DEFAULT_POLL_S
    if v != v or v in (float("inf"), float("-inf")):
        return DEFAULT_POLL_S
    return max(v, MIN_POLL_S)


def _strip_dollars(rows):
    if rows is None:
        return None
    out = []
    for r in rows:
        r = {k: v for k, v in r.items() if k not in DOLLAR_KEYS}
        r["unknown"] = [u for u in r.get("unknown", []) if u not in DOLLAR_KEYS]
        out.append(r)
    return out


class RhState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.polled = False
        self.as_of = None
        self.error = None
        self.data = {k: None for k in SECTIONS}

    def flat(self) -> bool:
        """No known working order and no known pending approval. Unknown sections are ignored (see module doc)."""
        orders, appr = self.data.get("orders") or [], self.data.get("approvals") or []
        return not any(o.get("status") in WORKING for o in orders) and not any(a.get("status") == "pending" for a in appr)

    def snapshot(self, stream=False) -> dict:
        if not self.polled:
            return {"enabled": True, "connected": False}
        d = self.data
        unknown = [k for k in SECTIONS if d[k] is None]
        pos, ords = d["positions"], d["orders"]
        out = {"enabled": True, "connected": self.error != "unauthorized", "ok": not unknown and self.error is None,
               "as_of": self.as_of, "unknown": unknown, "stream": bool(stream), "account": d["account"],
               "positions": pos, "orders": ords, "approvals": d["approvals"], "approval_setting": d["approval_setting"],
               "pnl": d["pnl"]}
        if self.error:
            out["error"] = self.error
        if stream:                                   # Robinhood money values never reach a stream-safe view
            out["positions"], out["orders"] = _strip_dollars(pos), _strip_dollars(ords)
            out["pnl"] = None
        return out

    def ai_block(self, stream=False) -> dict:
        return self.snapshot(stream=stream)

    def apply(self, got: dict, error=None):
        self.polled = True
        self.as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.error = error
        self.data = {k: got.get(k) for k in SECTIONS}

    def __repr__(self):
        return f"RhState(polled={self.polled}, error={self.error!r})"


class Runtime:
    def __init__(self, reader: Reader, *, token_present=None, market_quiet=None, poll_s=DEFAULT_POLL_S, closer=None):
        self.reader = reader
        self.state = RhState()
        self.poll_s = max(float(poll_s), MIN_POLL_S)
        self._token_present = token_present or (lambda: True)
        self._market_quiet = market_quiet or px_tradeday.market_quiet
        self._closer = closer
        self._task = None

    def quiet(self) -> bool:
        return bool(self._market_quiet()) and self.state.flat()

    async def poll_once(self) -> bool:
        """One refresh. False (and no outbound call) when there is no token or the market is quiet and flat."""
        if not self._token_present():
            self.state.reset()
            return False
        if self.quiet():
            return False
        r, got, error = self.reader, {}, None
        steps = (("account", lambda: r.accounts()), ("positions", self._positions), ("orders", r.orders),
                 ("approvals", r.approvals), ("approval_setting", r.approval_setting), ("pnl", r.realized_pnl))
        try:
            for name, fn in steps:
                v = await fn()
                got[name] = v[0] if name == "account" else v
        except Unknown as e:
            error = e.code
        except McpError as e:
            error = e.code
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never a crash, never provider text
            error = "bad_reply"
        self.state.apply(got, error)
        return True

    async def _positions(self):
        return (await self.reader.positions()) + (await self.reader.option_positions())

    async def run(self):
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.state.apply({}, "bad_reply")
            await asyncio.sleep(self.poll_s)

    def start(self):
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._closer is not None:
            await self._closer()
            self._closer = None

    def __repr__(self):
        return f"Runtime(poll_s={self.poll_s}, state={self.state!r})"


def from_env(environ=None, transport=None, closer=None):
    """A Runtime when PX_RH is truthy, else None. Raises McpError for a URL that is neither https nor loopback http."""
    env = os.environ if environ is None else environ
    if not _truthy(env.get("PX_RH")):
        return None
    url = (env.get("PX_RH_URL") or "").strip() or ENDPOINT
    path = (env.get("PX_RH_TOKEN_FILE") or "").strip() or str(ROOT / "data" / "rh_tokens.json")
    store = TokenStore(path, base_url=url, transport=transport)
    client = Client(url, store, transport=transport)
    quiet_on = (env.get("PX_WEEKEND_QUIET") or "1").strip() != "0"
    return Runtime(Reader(client), token_present=lambda: store.load() is not None,
                   market_quiet=(px_tradeday.market_quiet if quiet_on else (lambda: False)),
                   poll_s=poll_interval(env), closer=closer)


async def open_from_env(environ=None, transport=None):
    """`from_env`, plus the in-process mock when PX_RH_MOCK=1 (dev only): it gets pre-issued tokens in
    PX_RH_TOKEN_FILE (default data/rh_tokens.mock.json, never the real token file) so the demo needs no login."""
    env = dict(os.environ if environ is None else environ)
    stop = None
    if _truthy(env.get("PX_RH")) and _truthy(env.get("PX_RH_MOCK")):
        from .. import dev_robinhood
        tf = (env.get("PX_RH_TOKEN_FILE") or "").strip() or str(ROOT / "data" / "rh_tokens.mock.json")
        url, stop = await dev_robinhood.start_in_process(tf)
        env["PX_RH_URL"], env["PX_RH_TOKEN_FILE"] = url, tf
    try:
        return from_env(env, transport=transport, closer=stop)
    except BaseException:
        if stop is not None:
            await stop()
        raise
