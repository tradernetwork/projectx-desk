"""MCP surface for the desk (FastMCP), mounted by px_app at /mcp/. Loopback, bearer token.

What an agent gets: data, sizing, and the ability to STAGE a ticket for the operator to review. What it
does NOT get, by construction and pinned by tests: place, flatten, cancel, clear-halt, or any tool
that moves money or lifts a safety. Tools never register those names at all (the host sees
`unknown tool`, not a refusal). `halt` is allowed because it can only reduce exposure.
"""
import hmac
import re
import time
from typing import Optional

import sizing
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

import px_digest
from px_state import order_view, position_view

CONTRACT_RE = re.compile(r"^CON\.[A-Za-z0-9._]{1,40}$")
PLACEHOLDERS = ("your_", "changeme", "change_me", "example", "placeholder", "secret", "token", "password")


def weak_token_reason(token: Optional[str]) -> Optional[str]:
    """Why a token must not be used, or None. Never quotes the value (this work is streamed)."""
    if not token:
        return "no token set"
    if len(token) < 32:
        return "token shorter than 32 characters"
    low = token.lower()
    if any(p in low for p in PLACEHOLDERS):
        return "token looks like a placeholder"
    if len(set(token)) < 12:
        return "token has too little variety (generate one with: openssl rand -hex 32)"
    return None


class BearerVerifier(TokenVerifier):
    """Constant-time static bearer check. One token, read scope only."""

    def __init__(self, token: str):
        super().__init__(required_scopes=["read"])
        self._token = token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not hmac.compare_digest(token or "", self._token):
            return None
        return AccessToken(token=token, client_id="px-agent", scopes=["read"], expires_at=None)


def _contract(c: Optional[str]):
    if c is not None and not CONTRACT_RE.match(c):
        raise ValueError("bad contract id")
    return c


def build_mcp(get_app, token: Optional[str] = None) -> FastMCP:
    """`get_app` returns the FastAPI app lazily (its state exists only after startup)."""
    mcp = FastMCP("projectx", auth=BearerVerifier(token) if token else None, instructions=(
        "ProjectX futures desk. You may read data, compute sizing and STAGE a ticket for the operator to review. "
        "You cannot place, flatten, cancel or approve anything; the operator places at the desk. "
        "Never do sizing math yourself: read the size fields. Unknown or stale data means do not suggest an entry."))

    def st():
        return get_app().state

    @mcp.tool
    async def get_digest(contract: Optional[str] = None, side: str = "long", risk_pct: float = 0.03,
                         stop_k: float = 1.5, stream: bool = False) -> str:
        """The whole desk as terse text: mode, feed, quote, VWAP, ATR/ADX, size idea, ticket, risk, GEX, positions."""
        _contract(contract)
        s = await px_digest.build_ai_state(get_app(), contract, side=side, risk_pct=risk_pct, stop_k=stop_k, stream=stream)
        return px_digest.format_digest(s)

    @mcp.tool
    async def get_state(contract: Optional[str] = None, side: str = "long", risk_pct: float = 0.03,
                        stop_k: float = 1.5, stream: bool = False) -> dict:
        """The same facts as get_digest, as JSON (schema v1)."""
        _contract(contract)
        return await px_digest.build_ai_state(get_app(), contract, side=side, risk_pct=risk_pct, stop_k=stop_k, stream=stream)

    @mcp.tool
    def get_positions() -> list:
        """Open positions (side, size, average price). Numbers and enums only."""
        return [position_view(p) for p in st().live.positions.values()]

    @mcp.tool
    def get_orders() -> list:
        """Working orders. `tag` is OWN (placed by this desk), EXTERNAL (anything else) or null."""
        return [order_view(o) for o in st().live.orders.values()]

    @mcp.tool
    async def get_quote(contract: str) -> dict:
        """Latest bid/ask/last and its age in seconds. age_s large or null means stale: do not trade on it."""
        _contract(contract)
        await st().live.ensure_quote(contract, wait=1.0)
        q = st().live.quotes.get(contract)
        if not q:
            return {"status": "NO_QUOTE"}
        return {"status": "OK", "last": q.get("lastPrice"), "bid": q.get("bestBid"), "ask": q.get("bestAsk"),
                "age_s": round(time.monotonic() - q.get("_rx", 0), 1)}

    @mcp.tool
    async def get_bars(contract: str, unit: int = 2, n: int = 5, limit: int = 60) -> dict:
        """Recent OHLCV bars, oldest first, compact. unit: 1=sec 2=min 3=hour 4=day. limit is capped at 120."""
        _contract(contract)
        if unit not in (1, 2, 3, 4) or not 1 <= n <= 60:
            raise ValueError("bad unit or n")
        limit = max(1, min(int(limit), 120))
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        secs = {1: 1, 2: 60, 3: 3600, 4: 86400}[unit] * n
        raw = await st().client.bars({"contractId": contract, "live": False, "unit": unit, "unitNumber": n,
                                      "limit": limit, "startTime": (now - timedelta(seconds=max(secs * limit * 3, 864000))).isoformat(),
                                      "endTime": now.isoformat(), "includePartialBar": True})
        rows = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in reversed(raw)]
        return {"cols": ["t", "o", "h", "l", "c", "v"], "rows": rows[-limit:]}

    @mcp.tool
    async def search_contracts(text: str) -> list:
        """Find contracts by symbol text (e.g. MES). Returns id, name, tick size and tick value."""
        if not re.fullmatch(r"[A-Za-z0-9 ]{1,20}", text or ""):
            raise ValueError("bad search text")
        cs = await st().client.search_contracts(text, live=False)
        return [{"id": c["id"], "name": c.get("name"), "tick": c.get("tickSize"), "tick_value": c.get("tickValue")}
                for c in cs if CONTRACT_RE.match(str(c.get("id", "")))]

    @mcp.tool
    async def get_risk_status() -> dict:
        """Daily-loss room, drawdown buffer, which one binds, order count and exposure. Dollar values may be null in stream mode."""
        s = await px_digest.build_ai_state(get_app(), None)
        return {"mode": s["mode"], "risk": s["risk"], "feed": s["feed"]}

    @mcp.tool
    async def size_trade(contract: str, side: str, stop_price: float, risk_pct: float = 0.03) -> dict:
        """What size would the desk allow for this idea? Pure calculation: stages nothing, always rounds down."""
        _contract(contract)
        app = get_app()
        g, live = app.state.guard, app.state.live
        await live.ensure_quote(contract, wait=1.0)
        lim = g.limits
        remaining, room = g.budget()
        if remaining is None:
            return {"status": "UNKNOWN", "reason": "P&L or loss limits unknown: no size"}
        rec = await g._contract(contract)
        q = live.quotes.get(contract) or {}
        ref = q.get("bestAsk" if side == "long" else "bestBid") or q.get("lastPrice")
        if not rec or not ref:
            return {"status": "UNKNOWN", "reason": "no contract record or quote"}
        budget, scale = await px_digest.scaled_risk(app, contract, risk_pct)
        try:
            d = sizing.describe_trade(contract, side, float(ref), float(stop_price), budget, risk_budget_dollars=remaining,
                                      max_contracts=lim.max_qty_order, live_tick_size=rec["tickSize"],
                                      live_tick_value=rec["tickValue"])
        except sizing.SizingError as e:
            return {"status": "ERROR", "reason": str(e)}
        return {"status": "OK", **{k: v for k, v in d.items()},
                "scale": {"multiplier": scale["multiplier"], "reasons": scale["reasons"]}}

    @mcp.tool
    async def preview_order(contract: str, side: str, stop_price: Optional[float] = None, risk_pct: float = 0.03,
                            order_type: str = "market", entry: Optional[float] = None,
                            stop_ticks: Optional[int] = None) -> dict:
        """STAGE a ticket for the operator. This does not place anything and you cannot place it. Tell the operator to review
        the readback and place it at the desk. Returns the checks that failed if the desk refuses."""
        _contract(contract)
        app = get_app()
        g, live = app.state.guard, app.state.live
        rd, _scale = await px_digest.scaled_risk(app, contract, risk_pct)
        if stop_price is None and stop_ticks is not None:
            stop_price = await g.stop_from_ticks(contract, (side or "").lower(), stop_ticks)
        p = await g.preview(contract_id=contract, side=side, stop_price=stop_price, risk_dollars=rd,
                            order_type=order_type, entry=entry, origin="mcp")
        out = {"ok": p.ok, "reasons": p.reasons}
        if p.ticket:
            out.update(ticket_id8=p.ticket.hash8, expires_in_s=max(0, round(p.ticket.expires - g.clock())),
                       readback=dict(p.ticket.readback),
                       next_step="The operator must review this ticket on the desk and place it personally. You cannot place it.")
        return out

    @mcp.tool
    def halt() -> dict:
        """Freeze all new opens immediately. Reduces exposure only; only the operator can clear it."""
        get_app().state.guard.trip_halt("mcp")
        return {"halted": True, "note": "Only the operator can clear the halt at the desk."}

    return mcp
