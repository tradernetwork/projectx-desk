"""Account-scoped Robinhood reads. Read tools only (the client refuses anything else).

Every call first resolves the MCP account (the one account flagged `agentic_allowed`, literally `True`) and drops every
other account before any data tool runs. Zero or several MCP accounts is `Unknown`, never a guess. The account number
goes into tool arguments and nowhere else: not into a view, not into an exception.
"""
from __future__ import annotations

from . import views


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

    async def _resolve(self):
        data = await self.client.call("get_accounts", {})
        mine = [a for a in _items(data, "accounts") if isinstance(a, dict) and a.get("agentic_allowed") is True]
        if len(mine) != 1:
            raise Unknown()
        number = mine[0].get("account_number")
        if not isinstance(number, str) or not number:
            raise Unknown()
        return number, mine[0]

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
