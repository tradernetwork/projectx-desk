#!/usr/bin/env python
"""Print the SHAPE of what Robinhood's read tools return, never a value: key names, types and list lengths only.

Read-only (calls only tools in `projectx_desk.rh.mcp.READ_TOOLS`). It uses the desk's token file (`PX_RH_TOKEN_FILE`, default
data/rh_tokens.json; sign in first with scripts/rh_capture.py). The account number is used only as a tool argument and is never printed.
Use it to check `rh/views.py` and the mock in `dev_robinhood.py` against the live reply shapes without putting balances or positions
in a terminal, a log or a chat.

    python scripts/rh_shapes.py                 # prints the skeletons, writes data/rh_shapes.json
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk.rh import mcp, tokens  # noqa: E402

MAX_ITEMS, MAX_DEPTH = 2, 6


def skeleton(v, depth=0):
    """Type-only view of a JSON value. Strings keep only a coarse class (never the text), so no id, symbol, price or name survives."""
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(v, dict):
        return {k: skeleton(x, depth + 1) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return {"list": len(v), "item": skeleton(v[0], depth + 1) if v else None}
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if v is None:
        return "null"
    if isinstance(v, str):
        s = v.strip()
        try:
            float(s)
            return "numeric-string"
        except ValueError:
            pass
        return "string" if len(s) > 0 else "empty-string"
    return type(v).__name__


async def main():
    path = os.environ.get("PX_RH_TOKEN_FILE") or str(ROOT / "data" / "rh_tokens.json")
    url = os.environ.get("PX_RH_URL") or mcp.ENDPOINT
    store = tokens.TokenStore(path, base_url=url)
    if store.load() is None:
        print("no token file: run scripts/rh_capture.py first")
        return 1
    client = mcp.Client(url, store)
    out = {}
    try:
        accts = await client.call("get_accounts", {})
        out["get_accounts"] = skeleton(accts)
        items = accts.get("items") if isinstance(accts, dict) else None
        if items is None and isinstance(accts, dict):
            for v in accts.values():
                if isinstance(v, list):
                    items = v
        mine = [a for a in (items or []) if isinstance(a, dict) and a.get("agentic_allowed") is True]
        out["_agentic_accounts"] = len(mine)
        if len(mine) != 1:
            print(json.dumps(out, indent=1))
            return 2
        acct = str(mine[0].get("account_number") or "")
        calls = [("get_equity_positions", {"account_number": acct}), ("get_equity_orders", {"account_number": acct}),
                 ("get_option_positions", {"account_number": acct}), ("get_option_orders", {"account_number": acct}),
                 ("get_trade_approvals", {"account_number": acct}), ("get_trade_approval_setting", {"account_number": acct}),
                 ("get_portfolio", {"account_number": acct}), ("get_realized_pnl", {"account_number": acct}),
                 ("get_equity_quotes", {"symbols": ["SPY"]}),
                 # LETF desk charts: market data, no account; the reply shape is what the desk's bar view reads
                 ("get_equity_historicals", {"symbols": ["SPY"], "interval": "day",
                                             "start_time": (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")})]
        for tool, args in calls:
            try:
                out[tool] = skeleton(await client.call(tool, args))
            except mcp.McpError as e:
                out[tool] = f"error:{e.code}"
    finally:
        await client.aclose()
    dest = ROOT / "data" / "rh_shapes.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    print(f"-> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
