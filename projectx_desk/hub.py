"""Minimal SignalR (JSON protocol over WebSocket) client for the ProjectX user and market hubs.

One class serves both hubs. It remembers its subscriptions and re-sends them after every
reconnect, and tells listeners when the stream was interrupted so they can re-seed from REST
(ticks or order events may have been missed). The JWT rides in the URL query string, which is
how SignalR WebSockets authenticate, so nothing here ever logs the URL.
"""
import asyncio
import json
import os
import time

import aiohttp

RS = "\x1e"  # SignalR record separator
def _hub_base():
    return os.getenv("PROJECTX_HUB_BASE", "wss://rtc.topstepx.com/hubs")   # override only for the offline mock gateway
RECEIVE_TIMEOUT = 45  # silent half-open socket detector (server pings far more often)
QUIET_POLL_S = 30.0          # how often a quiet hub re-checks whether it may reconnect


def err_text(e):
    """Exception -> log-safe string (no URL: it carries the token)."""
    return f"{type(e).__name__} {getattr(e, 'status', '')}".strip()


class SignalRHub:
    def __init__(self, client, name, url=None, session=None):
        self.client, self.name = client, name
        self.url = url or f"{_hub_base()}/{name}"
        self._session, self._owns_session = session, session is None
        self._subs = {}        # (target, args tuple) -> unsubscribe target or None
        self._handlers = {}    # event name -> [callables]
        self._listeners = []   # callables(kind) kind in {"up", "resync", "down"}
        self._ws, self._task = None, None
        self.up = False
        self.quiet = None      # callable() -> bool: while it is true a dropped hub does not try to reconnect (market closed and flat)
        self.last_msg = 0.0    # monotonic time of the last frame, for feed-age display

    # ---- wiring ---------------------------------------------------------------------------

    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def listen(self, fn):
        self._listeners.append(fn)

    def _notify(self, kind):
        for fn in self._listeners:
            try:
                fn(kind)
            except Exception as e:  # a bad listener must not kill the hub
                print(f"{self.name} hub listener: {err_text(e)}", flush=True)

    # ---- subscriptions --------------------------------------------------------------------

    async def _send(self, obj):
        try:
            if self._ws is not None and not self._ws.closed:
                await self._ws.send_str(json.dumps(obj) + RS)
        except (ConnectionResetError, RuntimeError):
            pass  # run() sees the dead socket and replays self._subs

    async def subscribe(self, target, *args, unsub_target=None):
        key = (target, tuple(args))
        if key in self._subs:
            return
        self._subs[key] = unsub_target
        await self._send({"type": 1, "target": target, "arguments": list(args)})

    async def unsubscribe(self, target, *args):
        key = (target, tuple(args))
        unsub = self._subs.pop(key, None)
        if unsub:
            await self._send({"type": 1, "target": unsub, "arguments": list(args)})

    # ---- lifecycle ------------------------------------------------------------------------

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"px-hub-{self.name}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session is not None and self._owns_session:
            await self._session.close()

    async def reconnect(self):
        """Drop the socket so _run() reconnects, replays every subscription and notifies "resync". For a session the gateway
        logged out: the socket stays open but the pushes stop, so nothing else would ever re-subscribe."""
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - a dead socket is what we wanted anyway
                pass

    def feed_age_s(self):
        return None if not self.last_msg else round(time.monotonic() - self.last_msg, 1)

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay, ever = 1, False
        while True:
            if self.quiet is not None and self._quiet_now():
                await asyncio.sleep(QUIET_POLL_S)
                continue
            t0, alive = time.monotonic(), False
            try:
                tok = await self.client.token()
                async with self._session.ws_connect(f"{self.url}?access_token={tok}", heartbeat=None,
                                                    receive_timeout=RECEIVE_TIMEOUT) as ws:
                    self._ws = ws
                    await ws.send_str(json.dumps({"protocol": "json", "version": 1}) + RS)
                    for (target, args) in list(self._subs):
                        await self._send({"type": 1, "target": target, "arguments": list(args)})
                    handshaken = False
                    while True:
                        m = await ws.receive()
                        if m.type != aiohttp.WSMsgType.TEXT:
                            raise ConnectionError(f"ws {m.type.name}")
                        self.last_msg = time.monotonic()
                        for rec in filter(None, m.data.split(RS)):
                            f = json.loads(rec)
                            if not handshaken:  # handshake reply: {} or {"error": ...}
                                handshaken = True
                                if f.get("error"):
                                    raise ConnectionError(f"handshake: {f['error']}")
                                alive, self.up = True, True
                                self._notify("resync" if ever else "up")
                                ever = True
                                continue
                            self._dispatch(f)
                            if f.get("type") == 6:
                                await self._send({"type": 6})
                            elif f.get("type") == 7:
                                raise ConnectionError(f"server closed hub: {f.get('error', '')}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"{self.name} hub: {err_text(e)}; retry in {delay}s", flush=True)
                if self.up:
                    self.up = False
                    self._notify("down")
                if isinstance(e, aiohttp.WSServerHandshakeError) and e.status in (401, 403):
                    await self.client.token(fresh=True)  # only auth failures justify a fresh login
            finally:
                self._ws = None
            if alive and time.monotonic() - t0 > 30:
                delay = 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def _quiet_now(self):
        try:
            return bool(self.quiet())
        except Exception:       # a broken predicate must never stop the hub from reconnecting
            return False

    def _dispatch(self, f):
        if f.get("type") == 1:
            for fn in self._handlers.get(f.get("target"), ()):
                try:
                    fn(*f.get("arguments", []))
                except Exception as e:  # one malformed event must not kill the stream
                    print(f"{self.name} hub handler {f.get('target')}: {err_text(e)}", flush=True)
        elif f.get("type") == 3 and f.get("error"):
            print(f"{self.name} hub completion error: {f['error']}", flush=True)
