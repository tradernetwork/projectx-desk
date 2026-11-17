"""A mock Robinhood Trading MCP server for offline development, demos and tests.

Serves Streamable-HTTP JSON-RPC at POST /mcp plus stub OAuth endpoints. Every number, id and symbol is invented; the
shapes follow the synthetic fixtures of tradermatrix-desktop (numbers as strings, `{"data": ...}` envelopes) and are
UNVERIFIED against the real service. Write tools are listed (so the capture and drift scripts have something to
compare) and, from phase 1, ACT on this in-memory mock only: `place_equity_order` records an order and a pending approval
(approvals ON) or a working order (OFF), `cancel_equity_order` cancels it, and `review_equity_order` echoes a review with a
`ref_id`. Nothing here can reach a real broker. Knobs on `app.state.mock`: `setting_on`, `setting_reply` ("missing"),
`place_reply` (None, "drop" = order recorded then HTTP 500, "badjson", "no_state", "live" = approvals not honoured, "fail" =
tool error before anything is created), `quote_age_s`, `quote_ts` (False omits it), `quote_reply` ("empty"), `quote_clock`,
`orders_extra`, `approvals_extra`, `place_calls` (arguments of every recorded place) and the `approve/decline/expire/fill`
helpers that move a pending approval.

`make_app(accounts=None)` returns a Starlette app. `app.state.mock` exposes knobs for tests: `fault` (None, "redirect",
"huge", "badjson", "rate", "500"), `access_ttl`, `calls` (list of tool names called) and `requests` (count of /mcp posts).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
import urllib.parse
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

PROTOCOL_VERSION = "2025-06-18"

DEFAULT_ACCOUNTS = [
    {"account_number": "MOCK-MCP-0001", "type": "limited_margin", "is_default": False, "agentic_allowed": True,
     "management_type": "agentic", "state": "active", "option_level": "option_level_2"},
    {"account_number": "MOCK-PERSONAL-0002", "type": "margin", "is_default": True, "agentic_allowed": False,
     "nickname": "personal", "state": "active", "option_level": "option_level_2"},
]

# (name, description, required inputs)
READ_TOOL_DEFS = [
    ("get_accounts", "List brokerage accounts.", []),
    ("get_portfolio", "Portfolio summary for an account.", ["account_number"]),
    ("get_equity_positions", "Equity positions.", ["account_number"]),
    ("get_equity_orders", "Equity orders.", ["account_number"]),
    ("get_equity_quotes", "Equity quotes.", ["symbols"]),
    ("get_option_positions", "Option positions.", ["account_number"]),
    ("get_option_orders", "Option orders.", ["account_number"]),
    ("get_option_chains", "Option chains.", ["symbol"]),
    ("get_realized_pnl", "Realized P&L.", ["account_number"]),
    ("get_trade_approvals", "Pending trade approvals.", ["account_number"]),
    ("get_trade_approval_setting", "Whether a human must approve trades.", ["account_number"]),
]
WRITE_TOOL_DEFS = [
    ("review_equity_order", "Preview an equity order.", ["account_number", "symbol", "side", "quantity"]),
    ("place_equity_order", "Place an equity order.", ["account_number", "symbol", "side", "quantity"]),
    ("cancel_equity_order", "Cancel an equity order.", ["account_number", "order_id"]),
    ("place_option_order", "Place an option order.", ["account_number"]),
    ("cancel_option_order", "Cancel an option order.", ["account_number", "order_id"]),
    ("exercise_option", "Exercise an option position.", ["account_number"]),
    ("decline_trade_approval", "Decline a pending approval.", ["approval_id"]),
]


def _tool_list():
    out = []
    for name, desc, req in READ_TOOL_DEFS + WRITE_TOOL_DEFS:
        out.append({"name": name, "description": desc, "inputSchema": {
            "type": "object", "properties": {k: {"type": "string"} for k in req}, "required": req}})
    return out


def _data(payload):
    return {"data": payload, "guide": "mock"}


def _tool_payload(name, args, accounts, st=None):
    if name == "get_accounts":
        return {"accounts": accounts}
    if name == "get_portfolio":
        return {"total_value": "5000.00", "equity_value": "3000.00", "options_value": "500.00", "cash": "1500.00",
                "currency": "USD", "buying_power": {"buying_power": "1500.00"}}
    if name == "get_equity_positions":
        return {"positions": [
            {"symbol": "AAA", "quantity": "10.0000", "average_buy_price": "100.5000", "type": "long"},
            {"symbol": "BBB", "quantity": "25.0000", "average_buy_price": "20.2500", "type": "long"},
            {"symbol": "CCC", "quantity": "3.0000", "average_buy_price": "310.0000", "type": "long"},
        ], "next": ""}
    if name == "get_option_positions":
        return {"positions": [
            {"symbol": "AAA", "chain_symbol": "AAA", "quantity": "1", "type": "long", "average_price": "2.40",
             "expiration_date": "2026-12-18", "strike_price": "105.00", "option_type": "call"},
            {"symbol": "BBB", "chain_symbol": "BBB", "quantity": "2", "type": "short", "average_price": "0.85",
             "expiration_date": "2026-12-18", "strike_price": "18.00", "option_type": "put"},
        ], "next": ""}
    if name == "get_equity_orders":
        return {"orders": (st.orders_extra if st else []) + [
            {"id": "mock-eo-1", "symbol": "AAA", "side": "buy", "type": "limit", "state": "filled",
             "quantity": "10.00000000", "price": "100.50000000", "average_price": "100.45"},
            {"id": "mock-eo-2", "symbol": "BBB", "side": "sell", "type": "limit", "state": "queued",
             "quantity": "5.00000000", "price": "22.00000000"},
        ], "next": ""}
    if name == "get_option_orders":
        return {"orders": [], "next": ""}
    if name == "get_equity_quotes":
        if st is not None and st.quote_reply == "empty":
            return {"results": []}
        rows = []
        for sym in (args.get("symbols") or []):
            q = {"symbol": sym, "last_trade_price": "101.25", "bid_price": "101.20", "ask_price": "101.30",
                 "previous_close": "100.00"}
            if st is None or st.quote_ts:
                q["updated_at"] = datetime.fromtimestamp((st.quote_clock() if st else time.time())
                                                         - (st.quote_age_s if st else 0), timezone.utc).isoformat()
            rows.append({"quote": q})
        return {"results": rows}
    if name == "get_option_chains":
        return {"chains": [{"id": "chain-1", "symbol": args.get("symbol", "AAA"),
                            "expiration_dates": ["2026-12-18", "2027-01-15"], "trade_value_multiplier": "100.0000"}]}
    if name == "get_realized_pnl":
        return {"window": "month", "data_points": [{"realized_gain": "12.50", "number_of_trades": 3}],
                "total_returns": "12.50"}
    if name == "get_trade_approvals":
        return {"approvals": (st.approvals_extra if st else []) + [
            {"approval_id": "mock-appr-1", "asset_class": "ASSET_CLASS_EQUITY", "state": "APPROVAL_STATE_PENDING",
             "proposed_order": {"symbol": "AAA", "side": "buy", "quantity": "1"}}], "next": ""}
    if name == "get_trade_approval_setting":
        if st is not None and st.setting_reply == "missing":
            return {"setting": {}}
        return {"setting": {"human_must_approve_trades": True if st is None else st.setting_on}}
    return None


class MockState:
    def __init__(self, accounts):
        self.accounts = accounts
        self.fault = None
        self.access_ttl = 3600
        self.calls = []
        self.requests = 0
        self.clients = {}            # client_id -> redirect_uris
        self.codes = {}              # code -> (client_id, challenge, redirect_uri)
        self.access = {}             # access token -> expiry
        self.refresh = {}            # live refresh token -> client_id (single use)
        self.session = "mock-session-" + secrets.token_hex(4)
        # phase 1 write behaviour (in-memory only)
        self.setting_on = True
        self.setting_reply = None
        self.place_reply = None
        self.quote_age_s = 0
        self.quote_ts = True
        self.quote_reply = None
        self.quote_clock = time.time
        self.orders_extra = []        # dicts in get_equity_orders shape; newest first
        self.approvals_extra = []
        self.place_calls = []         # arguments of every recorded place (account number included: it is the mock's)
        self.review_calls = []
        self.cancel_calls = []

    def place_for(self, number) -> bool:
        """True when an order for this account number is accepted: the account exists and is flagged agentic_allowed."""
        return any(a.get("account_number") == number and a.get("agentic_allowed") is True for a in self.accounts)

    def _find(self, ref_id):
        o = next((o for o in self.orders_extra if o.get("ref_id") == ref_id), None)
        a = next((a for a in self.approvals_extra if a.get("ref_id") == ref_id), None)
        return o, a

    def approve(self, ref_id, fill=False):
        o, a = self._find(ref_id)
        a["state"] = "APPROVAL_STATE_APPROVED"
        o["state"] = "filled" if fill else "confirmed"

    def decline(self, ref_id):
        o, a = self._find(ref_id)
        a["state"] = "APPROVAL_STATE_DECLINED"
        o["state"] = "cancelled"

    def expire(self, ref_id):
        o, a = self._find(ref_id)
        a["state"] = "APPROVAL_STATE_EXPIRED"
        o["state"] = "cancelled"

    def fill(self, ref_id):
        o, _ = self._find(ref_id)
        o["state"] = "filled"

    def record_place(self, args):
        """What a place does on the mock. Approvals ON: a pending approval and an unconfirmed order. OFF (or place_reply
        'live'): a working order, no approval. Returns (reply_state, order)."""
        self.place_calls.append(dict(args))
        n = len(self.orders_extra) + 1
        live = (not self.setting_on) or self.place_reply == "live"
        order = {"id": f"mock-new-{n}", "ref_id": args.get("ref_id"), "symbol": args.get("symbol"),
                 "side": args.get("side"), "type": "limit", "state": "queued" if live else "unconfirmed",
                 "quantity": str(args.get("quantity")), "price": str(args.get("limit_price"))}
        self.orders_extra.insert(0, order)
        if not live:
            self.approvals_extra.insert(0, {
                "approval_id": f"mock-new-appr-{n}", "ref_id": args.get("ref_id"), "asset_class": "ASSET_CLASS_EQUITY",
                "state": "APPROVAL_STATE_PENDING",
                "proposed_order": {"symbol": args.get("symbol"), "side": args.get("side"),
                                   "quantity": str(args.get("quantity")), "ref_id": args.get("ref_id")}})
        return ("placed" if live else "awaiting_approval"), order

    def issue(self, client_id):
        a, r = "mock-at-" + secrets.token_hex(8), "mock-rt-" + secrets.token_hex(8)
        self.access[a] = time.time() + self.access_ttl
        self.refresh[r] = client_id
        return {"access_token": a, "refresh_token": r, "token_type": "Bearer", "expires_in": self.access_ttl,
                "scope": "internal"}


def make_app(accounts=None):
    st = MockState(copy.deepcopy(DEFAULT_ACCOUNTS if accounts is None else accounts))

    def origin(request: Request):
        return f"{request.url.scheme}://{request.headers.get('host', 'localhost')}"

    async def metadata(request: Request):
        o = origin(request)
        return JSONResponse({"issuer": o, "authorization_endpoint": o + "/authorize", "token_endpoint": o + "/token",
                             "registration_endpoint": o + "/register",
                             "code_challenge_methods_supported": ["S256"]})

    async def register(request: Request):
        body = await request.json()
        uris = body.get("redirect_uris") or []
        if not uris or any(not str(u).startswith(("http://localhost", "http://127.0.0.1")) for u in uris):
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        cid = "mock-client-" + secrets.token_hex(4)
        st.clients[cid] = list(uris)
        return JSONResponse({"client_id": cid, "redirect_uris": uris}, status_code=201)

    async def authorize(request: Request):
        q = request.query_params
        cid, redirect = q.get("client_id", ""), q.get("redirect_uri", "")
        if cid not in st.clients or redirect not in st.clients[cid] or q.get("code_challenge_method") != "S256":
            return PlainTextResponse("bad request", status_code=400)
        code = "mock-code-" + secrets.token_hex(8)
        st.codes[code] = (cid, q.get("code_challenge", ""), redirect)
        sep = "&" if "?" in redirect else "?"
        loc = f"{redirect}{sep}code={code}&state={urllib.parse.quote(q.get('state', ''))}"
        return RedirectResponse(loc, status_code=302)

    async def token(request: Request):
        form = urllib.parse.parse_qs((await request.body()).decode())
        f = {k: v[0] for k, v in form.items()}
        grant = f.get("grant_type")
        if grant == "authorization_code":
            entry = st.codes.pop(f.get("code", ""), None)          # single use
            if not entry:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            cid, challenge, redirect = entry
            digest = base64.urlsafe_b64encode(hashlib.sha256(f.get("code_verifier", "").encode()).digest())
            if f.get("client_id") != cid or f.get("redirect_uri") != redirect or digest.rstrip(b"=").decode() != challenge:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(st.issue(cid))
        if grant == "refresh_token":
            cid = st.refresh.pop(f.get("refresh_token", ""), None)  # rotating: the old one dies here
            if cid is None or f.get("client_id") != cid:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(st.issue(cid))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    def rpc_ok(rid, result):
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers={"Mcp-Session-Id": st.session})

    def tool_error(rid, text):
        return rpc_ok(rid, {"isError": True, "content": [{"type": "text", "text": text}]})

    def ok_env(rid, payload):
        env = _data(payload)
        return rpc_ok(rid, {"content": [{"type": "text", "text": json.dumps(env)}], "structuredContent": env})

    def write(rid, name, args):
        if name not in ("place_equity_order", "cancel_equity_order", "review_equity_order"):
            return tool_error(rid, "write_disabled_in_mock")
        if not st.place_for(args.get("account_number")):
            return tool_error(rid, "account_not_allowed")
        if name == "review_equity_order":
            st.review_calls.append(dict(args))
            return ok_env(rid, {"review": {"symbol": args.get("symbol"), "side": args.get("side"),
                                           "quantity": str(args.get("quantity")),
                                           "ref_id": args.get("ref_id") or "mock-ref-" + secrets.token_hex(4)}})
        if name == "cancel_equity_order":
            st.cancel_calls.append(dict(args))
            for o in st.orders_extra:
                if o.get("id") == args.get("order_id"):
                    o["state"] = "cancelled"
                    return ok_env(rid, {"order": {"id": o["id"], "state": "cancelled"}})
            return tool_error(rid, "order_not_found")
        if st.place_reply == "fail":
            return tool_error(rid, "rejected_by_mock")
        reply_state, order = st.record_place(args)
        if st.place_reply == "drop":
            return PlainTextResponse("provider internal detail SECRET-TEXT", status_code=500)
        if st.place_reply == "badjson":
            return Response(b"{not json", media_type="application/json")
        body = {"order": {"id": order["id"], "ref_id": order["ref_id"], "state": reply_state}}
        if st.place_reply == "no_state":
            body = {"order": {"id": order["id"], "ref_id": order["ref_id"]}}
        return ok_env(rid, body)

    async def mcp(request: Request):
        st.requests += 1
        fault = st.fault
        if fault == "redirect":
            return RedirectResponse("http://evil.invalid/steal", status_code=302)
        if fault == "rate":
            return PlainTextResponse("slow down", status_code=429)
        if fault == "500":
            return PlainTextResponse("provider internal detail SECRET-TEXT", status_code=500)
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth.lower().startswith("bearer ") else ""
        if st.access.get(tok, 0) <= time.time():
            return PlainTextResponse("unauthorized", status_code=401)
        if fault == "huge":
            return Response(b"x" * (2 * 1024 * 1024), media_type="application/json")
        if fault == "badjson":
            return Response(b"{not json", media_type="application/json")
        try:
            msg = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse"}})
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if rid is None:
            return Response(status_code=202)                       # notification
        if method == "initialize":
            return rpc_ok(rid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                "serverInfo": {"name": "mock-robinhood-trading", "version": "0.1"}})
        if method == "tools/list":
            return rpc_ok(rid, {"tools": _tool_list()})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            st.calls.append(name)
            if name in {n for n, _, _ in WRITE_TOOL_DEFS}:
                return write(rid, name, args)
            payload = _tool_payload(name, args, st.accounts, st)
            if payload is None:
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown tool"}})
            env = _data(payload)
            return rpc_ok(rid, {"content": [{"type": "text", "text": json.dumps(env)}], "structuredContent": env})
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}})

    app = Starlette(routes=[
        Route("/.well-known/oauth-authorization-server", metadata),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize),
        Route("/token", token, methods=["POST"]),
        Route("/mcp", mcp, methods=["POST"]),
    ])
    app.state.mock = st
    return app


async def start_in_process(token_file, accounts=None, host="127.0.0.1"):
    """Serve the mock on a free loopback port inside the running event loop and pre-issue a token pair into
    `token_file` (a TokenStore file, mode 0600) so a demo needs no login. Returns `(mcp_url, stop)`; `await stop()`
    shuts it down. Dev and tests only: loopback by construction, no order path exists in the mock."""
    import asyncio
    import socket

    import uvicorn

    from .rh.tokens import Tokens, TokenStore

    app = make_app(accounts)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.get_running_loop().create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started or task.done():
            break
        await asyncio.sleep(0.025)
    if not server.started:
        server.should_exit = True
        sock.close()
        raise RuntimeError("mock robinhood failed to start")
    url = f"http://{host}:{sock.getsockname()[1]}/mcp"
    pair = app.state.mock.issue("mock-client")
    TokenStore(token_file, base_url=url).save(
        Tokens(pair["access_token"], pair["refresh_token"], time.time() + pair["expires_in"], "mock-client"))

    async def stop():
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            task.cancel()
        sock.close()

    return url, stop
