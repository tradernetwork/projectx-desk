"""Rotating OAuth token store for the Robinhood MCP.

Refresh tokens are single use: the server kills the old one the moment it answers a refresh. So the NEW pair is
saved (atomic, mode 0600) BEFORE the new access token is used, and if that save fails the new pair is kept in memory
and retried, while the spent token is never sent again. A failed refresh or save reads as `unauthorized` (read-down),
never as a crash, and nothing here logs. `Tokens.__repr__` hides the secrets.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from dataclasses import dataclass, field

from .mcp import McpError

REFRESH_SKEW_S = 60


@dataclass(frozen=True)
class Tokens:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires_at: float
    client_id: str

    def __repr__(self):
        return f"Tokens(client_id={self.client_id!r}, expires_at={self.expires_at}, <secrets hidden>)"


def _parse(raw) -> Tokens | None:
    try:
        d = json.loads(raw)
        a, r, c, e = d["access"], d["refresh"], d["client_id"], d["expires_at"]
        if not (isinstance(a, str) and isinstance(r, str) and isinstance(c, str) and a and r and c):
            return None
        if isinstance(e, bool) or not isinstance(e, (int, float)) or e != e or e in (float("inf"), float("-inf")):
            return None
        return Tokens(a, r, float(e), c)
    except (ValueError, KeyError, TypeError):
        return None


class TokenStore:
    def __init__(self, path, *, base_url: str | None = None, transport=None, now=time.time):
        self.path = pathlib.Path(path)
        self.base_url, self.transport, self.now = base_url, transport, now
        self._cur: Tokens | None = None          # newest known pair (may be ahead of the file after a failed save)
        self._unsaved = False                    # True when `_cur` could not be persisted yet
        self._lock = asyncio.Lock()

    # ---- file ------------------------------------------------------------------------------------------------
    def load(self) -> Tokens | None:
        try:
            return _parse(self.path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def save(self, tokens: Tokens) -> None:
        """Atomic: a temp file created 0600, fsynced, then os.replace. Raises OSError on failure, old file intact."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        try:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                print(json.dumps({"access": tokens.access, "refresh": tokens.refresh,
                                  "expires_at": tokens.expires_at, "client_id": tokens.client_id}), file=f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        self._cur, self._unsaved = tokens, False

    # ---- provider --------------------------------------------------------------------------------------------
    def _current(self) -> Tokens:
        if self._cur is None:
            self._cur = self.load()
        if self._cur is None:
            raise McpError("unauthorized")
        return self._cur

    async def access_token(self) -> str:
        async with self._lock:
            cur = self._current()
            if self._unsaved:
                self._try_persist(cur)
                if self._unsaved:
                    raise McpError("unauthorized")
            if cur.expires_at - self.now() > REFRESH_SKEW_S:
                return cur.access
            return (await self._refresh(cur)).access

    async def force_refresh(self) -> str:
        async with self._lock:
            return (await self._refresh(self._current())).access

    def _try_persist(self, tokens: Tokens):
        try:
            self.save(tokens)
        except OSError:
            self._unsaved = True

    async def _refresh(self, cur: Tokens) -> Tokens:
        if self._unsaved:
            # the file still holds the spent refresh token; the live pair is in memory. Do not refresh again, and do not hand out a token we could not persist.
            self._try_persist(cur)
            if self._unsaved:
                raise McpError("unauthorized")
            return cur
        if not self.base_url:
            raise McpError("unauthorized")
        from . import oauth
        try:
            new = await oauth.refresh_tokens(self.base_url, cur, transport=self.transport, now=self.now)
        except McpError:
            raise
        except Exception:
            raise McpError("unauthorized") from None
        self._cur, self._unsaved = new, True      # the old refresh token is spent as of now; never reuse it
        self._try_persist(new)
        if self._unsaved:
            raise McpError("unauthorized")        # read-down: could not persist, so do not run ahead of the disk
        return new
