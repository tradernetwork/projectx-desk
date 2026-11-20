"""Account-scoped Robinhood reads. Read tools only (the client refuses anything else).

Every call first resolves the MCP account (the one account flagged `agentic_allowed`, literally `True`) and drops every
other account before any data tool runs. Zero or several MCP accounts is `Unknown`, never a guess. The account number
goes into tool arguments and nowhere else: not into a view, not into an exception.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import tradeday as px_tradeday
from . import views
from .mcp import McpError


class Unknown(Exception):
    """The MCP account could not be resolved to exactly one. Reads as UNKNOWN, never as empty."""

    def __init__(self, code="account_unknown"):
        super().__init__(code)
        self.code = code


def _items(data, *keys):
    if isinstance(data, dict):
        for k in keys + ("items",):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


class Reader:
    def __init__(self, client):
        self.client = client
        self._cycle = None

    @contextmanager
    def cycle(self):
        """Inside this block the MCP account is resolved once (one get_accounts call per poll). A fresh block
        re-resolves. A failed or ambiguous resolution raises and is never cached."""
        self._cycle = {}
        try:
            yield
        finally:
            self._cycle = None

    async def _resolve(self):
        if self._cycle is not None and "hit" in self._cycle:
            return self._cycle["hit"]
        hit = await self._resolve_fresh()
        if self._cycle is not None:
            self._cycle["hit"] = hit
        return hit

    async def _resolve_fresh(self):
        data = await self.client.call("get_accounts", {})
        mine = [a for a in _items(data, "accounts") if isinstance(a, dict) and a.get("agentic_allowed") is True]
        if len(mine) != 1:
            raise Unknown()
        number = mine[0].get("account_number")
        if not isinstance(number, str) or not number:
            raise Unknown()
        return number, mine[0]

    async def write_account(self):
        """The resolved MCP account number, for `rh/guard.py` to put in a tool argument and nowhere else (never a view,
        an audit line, a log or an exception). Zero or several MCP accounts raises Unknown."""
        number, _ = await self._resolve()
        return number

    @staticmethod
    def _quote_row(q):
        """{bid, ask, last, updated_at} from one quote dict; a number or timestamp that is missing or bad is None. updated_at is
        epoch seconds of the NEWEST of venue_bid_time, venue_ask_time and venue_last_trade_time (the live quote has no
        `updated_at`; checked against the real Trading MCP 2026-11-17), so a quote whose bid and ask have not moved reads as old."""
        stamps = []
        for k in ("venue_bid_time", "venue_ask_time", "venue_last_trade_time", "updated_at"):
            try:
                if q.get(k):
                    stamps.append(px_tradeday.parse_ts(q[k]).timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
        ts = max(stamps) if stamps else None
        return {"bid": views.num(q.get("bid_price")), "ask": views.num(q.get("ask_price")),
                "last": views.num(q.get("last_trade_price")), "updated_at": ts}

    async def quote(self, symbol):
        """{bid, ask, last, updated_at} for one symbol (see `_quote_row`). An unreadable or empty reply is None. The order guard
        reads this: its shape and behaviour must not change."""
        data = await self.client.call("get_equity_quotes", {"symbols": [symbol]})
        for row in _items(data, "results"):
            q = row.get("quote") if isinstance(row, dict) else None
            if isinstance(q, dict) and views.symbol(q.get("symbol")) == symbol:
                return self._quote_row(q)
        return None

    async def quotes(self, symbols):
        """Market-data quotes for a few symbols in ONE call (the LETF desk): {symbol: {bid, ask, last, prev_close, updated_at}}.
        No account is resolved. `prev_close` is the quote's `previous_close` (or `adjusted_previous_close`), else None: the
        location of the official close in the live reply is UNVERIFIED. A symbol the reply does not carry is absent."""
        want = [s for s in dict.fromkeys(symbols) if views.symbol(s)]
        if not want:
            return {}
        data = await self.client.call("get_equity_quotes", {"symbols": want})
        out = {}
        for row in _items(data, "results"):
            q = row.get("quote") if isinstance(row, dict) else None
            sym = views.symbol(q.get("symbol")) if isinstance(q, dict) else None
            if sym in want and sym not in out:
                r = self._quote_row(q)
                r["prev_close"] = views.num(q.get("previous_close"))
                if r["prev_close"] is None:
                    r["prev_close"] = views.num(q.get("adjusted_previous_close"))
                out[sym] = r
        return out

    async def bars(self, symbols, interval, start_iso, _retry=True):
        """Completed OHLCV bars for a few symbols in ONE call (the LETF desk): {symbol: {"bars": [view...], "dropped": n}}.
        Regular hours, split adjusted. No account is resolved. Bars are `views.bar_view` rows, sorted, de-duplicated by time and
        capped at 1000 per symbol (the newest). A reply over the client's 1 MB cap (`too_big`) is retried ONCE with the first half
        of the range dropped; a second failure propagates as McpError. Symbols the reply does not carry are absent."""
        want = [s for s in dict.fromkeys(symbols) if views.symbol(s)]
        if not want:
            return {}
        try:
            data = await self.client.call("get_equity_historicals", {
                "symbols": want, "interval": interval, "start_time": start_iso,
                "bounds": "regular", "adjustment_type": "split"})
        except McpError as e:
            if e.code != "too_big" or not _retry:
                raise
            t0 = px_tradeday.parse_ts(start_iso).timestamp()
            half = datetime.fromtimestamp(t0 + (time.time() - t0) / 2, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return await self.bars(symbols, interval, half, _retry=False)
        out = {}
        for res in _items(data, "results"):
            sym = views.symbol(res.get("symbol")) if isinstance(res, dict) else None
            if sym not in want or sym in out:
                continue
            raw = res.get("bars") if isinstance(res.get("bars"), list) else []
            rows = [v for v in (views.bar_view(b) for b in raw) if v is not None]
            by_t = {r[0]: r for r in rows}
            ordered = [by_t[t] for t in sorted(by_t)]
            out[sym] = {"bars": ordered[-1000:], "dropped": len(raw) - len(rows)}
        return out

    async def accounts(self):
        """Views of the MCP account only (a list of one)."""
        _, raw = await self._resolve()
        return [views.account_view(raw)]

    async def _list(self, tool, key, view):
        acct, _ = await self._resolve()
        data = await self.client.call(tool, {"account_number": acct})
        return [v for v in (view(r) for r in _items(data, key)) if v is not None]

    async def positions(self):
        return await self._list("get_equity_positions", "positions", views.position_view)

    async def option_positions(self):
        return await self._list("get_option_positions", "positions", views.position_view)

    async def orders(self):
        return await self._list("get_equity_orders", "orders", views.order_view)

    async def approvals(self):
        return await self._list("get_trade_approvals", "approvals", views.approval_view)

    async def approval_setting(self):
        acct, _ = await self._resolve()
        data = await self.client.call("get_trade_approval_setting", {"account_number": acct})
        s = data.get("setting") if isinstance(data, dict) else None
        v = s.get("human_must_approve_trades") if isinstance(s, dict) else None
        return {"human_must_approve": v if isinstance(v, bool) else None}

    async def realized_pnl(self):
        acct, _ = await self._resolve()
        data = await self.client.call("get_realized_pnl", {"account_number": acct})
        d = data if isinstance(data, dict) else {}
        return {"window": views.enum(d.get("window"), {"day", "week", "month", "year", "all"}) or "unknown",
                "realized": views.num(d.get("total_returns"))}
