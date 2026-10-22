"""ProjectX desk web app (FastAPI). Chart data, live ticks, the AI-readable surface, and the human desk actions (stage, place, flatten, stop, halt).

Run:  PROJECTX_USERNAME=... PROJECTX_API_KEY=... python px_app.py   ->  http://127.0.0.1:8600
Binds loopback only and rejects foreign Host / Origin headers (DNS-rebinding and drive-by guard).
Every order write lives in px_guard.py; the routes here only call it, and only a human click reaches place().
"""
import asyncio
import hmac
import json
import os
import pathlib
import re
import secrets
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from projectx_desk import digest as px_digest
from projectx_desk import grades as px_grades
from projectx_desk import indicators as px_ind
from projectx_desk import respect as px_respect
from projectx_desk import feed as px_feed
from projectx_desk import lock as px_lock
from projectx_desk import mcp as px_mcp
from projectx_desk import net as px_net
from projectx_desk.guard import Guard, GuardError
from projectx_desk.limits import Limits
from projectx_desk.client import live_data, PXAuthError, PXClient, PXError, PXNotConfigured, PXRateLimited
from projectx_desk.hub import SignalRHub
from projectx_desk.state import DOWN, RESYNC, LiveState, order_view, position_view, ts_ms
from projectx_desk.tradeday import session_start

from projectx_desk import PKG_DIR, ROOT
HERE = PKG_DIR                              # the UI (static/) ships inside the package
def _port():
    return int(os.getenv("CHART_PORT", 8600))   # read at call time so .env.projectx can set it
MAX_SSE = 16
CONTRACT_RE = re.compile(r"^CON\.[A-Za-z0-9._]{1,40}$")
UNIT_SECS = {1: 1, 2: 60, 3: 3600, 4: 86400, 5: 604800, 6: 2592000}


_live_data = live_data


def create_app(client=None, market_hub=None, user_hub=None, allowed_hosts=None, connect=True, guard=None, limits=None, mcp_token=None,
               operator_token=None, allowed_origins=None):
    """Build the app. Tests inject a fake client / hubs and set connect=False."""
    problem = px_net.bind_problem(os.getenv("PX_HOST", "127.0.0.1"), operator_token or os.getenv("PX_OPERATOR_TOKEN"),
                                  os.getenv("PX_ALLOWED_HOSTS"), px_mcp.weak_token_reason)
    if problem:       # same rule as main(), so an alternate launcher (uvicorn projectx_desk.app:...) cannot skip it
        raise SystemExit(problem)
    hosts = set(allowed_hosts) if allowed_hosts else px_net.allowed_hosts(_port(), os.getenv("PX_ALLOWED_HOSTS"))
    origins = set(allowed_origins) if allowed_origins else px_net.allowed_origins(hosts, os.getenv("PX_ALLOWED_ORIGINS"))
    op_token = operator_token if operator_token is not None else (os.getenv("PX_OPERATOR_TOKEN") or None)
    if op_token and px_mcp.weak_token_reason(op_token):
        raise SystemExit(f"PX_OPERATOR_TOKEN is not usable ({px_mcp.weak_token_reason(op_token)}); generate one with: openssl rand -hex 32")
    holder = {}   # the FastMCP server and its ASGI app, when a usable token is configured

    @asynccontextmanager
    async def lifespan(app):
        c = client or PXClient()
        m = market_hub or SignalRHub(c, "market")
        u = user_hub or SignalRHub(c, "user")
        app.state.client, app.state.market, app.state.user = c, m, u
        app.state.live = LiveState(m, u, flow_path=(os.getenv("PX_FLOW_FILE") or str(ROOT / "data" / "flow.json")) if connect else None)
        app.state.csrf = secrets.token_urlsafe(32)
        app.state.guard = guard or Guard(c, app.state.live, limits or Limits.from_env(), user_hub=u)
        # "Multiple sessions detected": the gateway logs this session out. Stop opening; reducing paths (flatten, cancel) stay available.
        u.on("GatewayLogout", lambda *a: app.state.guard.trip_halt("gateway_logout"))
        app.state.sse = 0
        app.state.boot_error = None
        app.state.bars_cache = {}
        async with AsyncExitStack() as stack:
            if holder.get("mcp_app") is not None:
                await stack.enter_async_context(holder["mcp_app"].lifespan(app))
            deadline = asyncio.create_task(_deadline_loop(app)) if connect else None
            if connect:
                try:
                    await _boot(app)
                except (PXError, KeyError, IndexError) as e:  # UI still loads; /state.json says why
                    app.state.boot_error = _boot_code(e)
            retry = asyncio.create_task(_boot_retry(app)) if connect and app.state.boot_error else None
            roots = px_ind.parse_roots(os.getenv("PX_TRACK_ROOTS")) if connect else []
            app.state.track_task = asyncio.create_task(_track_loop(app, roots)) if roots else None
            yield
            if app.state.track_task is not None:
                app.state.track_task.cancel()
            if retry is not None:
                retry.cancel()
            if deadline is not None:
                deadline.cancel()
        app.state.live.flow.save()               # a clean stop must not lose the last 30 s of tape (the file is rewritten every 30 s)
        for h in (m, u):
            await h.stop()
        if client is None:
            await c.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    tok = mcp_token if mcp_token is not None else os.getenv("PX_MCP_TOKEN")
    if tok:
        why = px_mcp.weak_token_reason(tok)
        if why:
            print(f"MCP not mounted: {why}", flush=True)
        else:
            srv = px_mcp.build_mcp(lambda: app, tok)
            holder["mcp"], holder["mcp_app"] = srv, srv.http_app(path="/")
            app.mount("/mcp", holder["mcp_app"])
            app.state.mcp = srv

    @app.middleware("http")
    async def operator_login(request: Request, call_next):
        """With PX_OPERATOR_TOKEN set, every route except /mcp (own bearer) and /healthz needs the login."""
        if op_token and px_net.needs_operator_login(request.url.path) \
                and not px_net.operator_ok(request.headers.get("authorization"), op_token):
            return JSONResponse({"detail": "operator login required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="projectx-desk", charset="UTF-8"'})
        return await call_next(request)

    @app.middleware("http")           # defined last, so it runs FIRST: a bad Host never reaches anything else
    async def host_guard(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "bad host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "bad origin"}, status_code=403)
        return await call_next(request)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    def contract_arg(c):
        if not CONTRACT_RE.match(c or ""):
            raise HTTPException(400, "bad contract")
        return c

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/contracts")
    async def contracts(request: Request, q: str = Query("", max_length=20)):
        try:
            cs = await request.app.state.client.search_contracts(q, live=_live_data())
        except PXRateLimited as e:
            raise HTTPException(429, headers={"Retry-After": str(e.retry_after)})
        except PXError as e:
            raise HTTPException(502, str(e))
        return [{"id": c["id"], "name": _safe_name(c.get("name"), c["id"]), "desc": "",   # the gateway's description is free text and is deliberately never forwarded
                 "tick": c["tickSize"], "tickValue": c.get("tickValue")} for c in cs]

    @app.get("/bars")
    async def bars(request: Request, contract: str, unit: int = 2, n: int = 5, limit: int = 1000,
                   end: int | None = None):
        contract_arg(contract)
        if unit not in UNIT_SECS or not 1 <= n <= 1000 or not 1 <= limit <= 20000:
            raise HTTPException(400, "out of range")
        cache = request.app.state.bars_cache
        key = (contract, unit, n, limit, end)
        hit = cache.get(key)
        if hit and time.time() - hit[0] < 2:  # also keeps polling far under 50 req / 30 s
            return hit[1]
        end_dt = datetime.fromtimestamp(end / 1000, timezone.utc) if end else datetime.now(timezone.utc)
        secs = UNIT_SECS[unit] * n
        # 3x covers closed sessions; 10 day floor spans weekends/holidays so short windows still fill `limit`
        start = end_dt - timedelta(seconds=min(max(secs * limit * 3, 10 * 86400), 3650 * 86400))
        try:
            raw = await request.app.state.client.bars({
                "contractId": contract, "live": _live_data(), "startTime": start.isoformat(),
                "endTime": end_dt.isoformat(), "unit": unit, "unitNumber": n, "limit": limit,
                "includePartialBar": not end})
        except PXRateLimited as e:
            raise HTTPException(429, headers={"Retry-After": str(e.retry_after)})
        except PXError as e:
            raise HTTPException(502, str(e))
        out = [{"timestamp": ts_ms(b["t"]), "open": b["o"], "high": b["h"], "low": b["l"],
                "close": b["c"], "volume": b["v"]} for b in reversed(raw)]  # API is newest-first
        if len(cache) >= 200:
            cache.pop(next(iter(cache)))
        cache[key] = (time.time(), out)
        return out

    @app.get("/api/respect")
    async def respect(request: Request, contract: str):
        """Read-only multi-timeframe 'what is price respecting' scan. Cached per closed bar; fixed error codes only."""
        cid = contract_arg(contract)
        try:
            return await px_digest.respect_result(request.app, cid)
        except px_digest.RespectError as e:
            hdr = {"Retry-After": "30"} if e.code == "RATE_LIMITED" else None
            return JSONResponse({"error": e.code}, status_code=429 if e.code == "RATE_LIMITED" else 502, headers=hdr)

    @app.get("/api/indicators")
    async def indicators(request: Request, contract: str = "", tf: str = ""):
        """Read-only: which indicators deserve the chart on this timeframe. Reuses the cached respect scan (no rescan, no
        extra bar fetch beyond what respect_result already does). Fixed codes only; input is never echoed."""
        if not CONTRACT_RE.match(contract or ""):
            return JSONResponse(px_ind.status_payload("error", "BAD_CONTRACT"), status_code=400)
        if tf not in px_respect.TIMEFRAMES:
            return JSONResponse(px_ind.status_payload("error", "BAD_TF"), status_code=400)
        try:
            ev = await _indicators_eval(request.app, contract, tf)
        except px_digest.RespectError as e:
            code = e.code if isinstance(e.code, str) and re.match(r"^[A-Z_]{1,24}$", e.code) else "UPSTREAM_ERROR"
            hdr = {"Retry-After": "30"} if code == "RATE_LIMITED" else None
            return JSONResponse(px_ind.status_payload("error", code, contract, tf),
                                status_code=429 if code == "RATE_LIMITED" else 502, headers=hdr)
        if ev is None:
            return px_ind.status_payload("loading", "", contract, tf)
        return px_ind.payload(contract, tf, ev)

    @app.get("/api/callouts")
    async def callouts(request: Request):
        """Read-only analyst futures callouts: allowlisted fields only, no message text. Cached ~20 s."""
        return await px_feed.feed_result(request.app)

    @app.get("/api/flow")
    async def flow(request: Request, contract: str):
        """Real buy/sell volume per minute from the trade tape. Coverage starts at `since`; `partial` minutes are unreliable."""
        cid = contract_arg(contract)
        return request.app.state.live.flow.snapshot(cid, int(time.time() * 1000))

    @app.get("/api/tickbars/config")
    async def tickbars_config(request: Request):
        """Configured tick-bar sizes (prints per bar). Numbers only."""
        return {"v": 1, "sizes": list(request.app.state.live.ticks.sizes)}

    @app.get("/api/tickbars")
    async def tickbars(request: Request, contract: str, n: int, limit: int = 500):
        """Tick bars built from the live trade tape: [t,o,h,l,c,v,buy,sell,gap,n] rows, forming bar last. `n` must be configured."""
        cid = contract_arg(contract)
        ticks = request.app.state.live.ticks
        if n not in ticks.sizes:
            return JSONResponse({"error": "bad_n"}, status_code=400)
        return ticks.snapshot(cid, n, limit)

    @app.get("/ticks")
    async def ticks(request: Request, contract: str):
        """SSE: `data: {t,p,v,s}` per trade; `event: resync` / `event: down` on interruption."""
        cid = contract_arg(contract)
        st = request.app.state
        if st.sse >= MAX_SSE:
            raise HTTPException(429, "too many streams")

        async def gen():
            st.sse += 1
            q = await st.live.subscribe_ticks(cid)
            try:
                while True:
                    try:
                        t = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        if await request.is_disconnected():
                            return
                        continue
                    if t is RESYNC:
                        yield "event: resync\ndata: {}\n\n"
                        continue
                    if t is DOWN:
                        yield "event: down\ndata: {}\n\n"
                        continue
                    try:
                        out = {"t": ts_ms(t["timestamp"]), "p": t["price"], "v": t["volume"], "s": t.get("type")}
                    except (KeyError, ValueError, TypeError):
                        continue  # one malformed tick must not kill the stream
                    yield f"data: {json.dumps(out)}\n\n"
            finally:
                st.sse -= 1
                await st.live.unsubscribe_ticks(cid, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/state.json")
    async def state_json(request: Request, contract: str | None = None, stream: int = 0):
        """Allowlisted view: numbers, enums and validated names only. No account id, no gateway free text."""
        st = request.app.state
        live = st.live
        q = live.quotes.get(contract) if contract and CONTRACT_RE.match(contract) else None
        keep = ("lastPrice", "bestBid", "bestAsk", "high", "low", "volume", "timestamp")
        return {
            "v": 0,
            "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "boot_error": st.boot_error,
            "feed": {"market_up": st.market.up, "user_up": st.user.up,
                     "market_age_s": st.market.feed_age_s(), "user_age_s": st.user.feed_age_s()},
            "seeded": {"positions": live.positions_seeded, "trades": live.trades_seeded},
            "account": {"can_trade": live.account.get("canTrade")} if stream else
                       {"can_trade": live.account.get("canTrade"), "simulated": live.account.get("simulated")},
            "halted": st.guard.halted(), "unknown_order": st.guard.unreconciled(),
            "quote": ({k: q.get(k) for k in keep} if q else None),
            "positions": [position_view(p) for p in live.positions.values()],
            "orders": [order_view(o) for o in live.orders.values()],
            "day_pnl": None if stream else live.day_pnl(),  # None = unknown (not seeded), never zero
        }


    # ---- AI-readable surface (read-only) ------------------------------------------------------

    async def _ai(request: Request, contract, side, risk_pct, stop_k, stream):
        if contract and not CONTRACT_RE.match(contract):
            raise HTTPException(400, "bad contract")
        if side not in ("long", "short") or not 0 < risk_pct <= 1 or not 0.1 <= stop_k <= 10:
            raise HTTPException(400, "bad parameters")
        return await px_digest.build_ai_state(request.app, contract, side=side, risk_pct=risk_pct,
                                              stop_k=stop_k, stream=bool(stream))

    @app.get("/ai/state")
    async def ai_state(request: Request, contract: str | None = None, side: str = "long", risk_pct: float = 0.05,
                       stop_k: float = 1.5, stream: int = 0):
        return await _ai(request, contract, side, risk_pct, stop_k, stream)

    @app.get("/ai/digest", response_class=PlainTextResponse)
    async def ai_digest(request: Request, contract: str | None = None, side: str = "long", risk_pct: float = 0.05,
                        stop_k: float = 1.5, stream: int = 0):
        return px_digest.format_digest(await _ai(request, contract, side, risk_pct, stop_k, stream))

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms():
        return px_digest.LLMS_TXT

    # ---- desk actions (POST): CSRF-protected, all money logic is in px_guard ------------------

    @app.get("/api/csrf")
    async def csrf(request: Request):
        return {"token": request.app.state.csrf}

    def post_gate(request: Request):
        """Every POST: Origin present and equal, JSON only, per-session token, not cross-site."""
        if request.headers.get("origin") not in origins:
            raise HTTPException(403, "origin required")
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise HTTPException(403, "cross-site")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "json only")
        if not hmac.compare_digest(request.headers.get("x-px-csrf", ""), request.app.state.csrf):
            raise HTTPException(403, "bad csrf token")

    async def body(request: Request):
        post_gate(request)
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "bad json")
        if not isinstance(b, dict):
            raise HTTPException(400, "object expected")
        return b

    def ticket_view(t, g):
        return {"ticket_id": t.ticket_id, "hash8": t.hash8, "state": t.state,
                "ttl_s": max(0, round(t.expires - g.clock())), "readback": dict(t.readback),
                "stop_plan": t.stop_plan["mode"], "account_suffix": t.account_suffix,
                "acked": t.acked, "stop_state": t.stop_state}

    @app.post("/api/preview")
    async def api_preview(request: Request):
        b = await body(request)
        g = request.app.state.guard
        try:
            if b.get("source") == "callout" and not g.limits.feed_stage:
                raise HTTPException(403, "callout staging is off (PX_FEED_STAGE=1 enables)")
            qty = b.get("qty")                      # manual BUY/SELL: the trader's own contracts; the risk is derived, every check still runs
            if qty is not None and (isinstance(qty, bool) or not isinstance(qty, int)):
                raise HTTPException(400, "qty must be a whole number")
            rd = b.get("risk_dollars")
            scale = None
            if qty is None and rd is None and b.get("risk_pct") is not None:
                rd, scale = await px_digest.scaled_risk(request.app, b.get("contract"), float(b["risk_pct"]))
            sp = b.get("stop_price")
            if sp is None and b.get("stop_ticks") is not None:
                sp = await g.stop_from_ticks(b.get("contract"), (b.get("side") or "").lower(), b["stop_ticks"])
            p = await g.preview(contract_id=b.get("contract"), side=b.get("side"), stop_price=sp, risk_dollars=rd,
                                order_type=b.get("order_type", "market"), entry=b.get("entry"), origin="ui", qty=qty)
        except (TypeError, ValueError, GuardError) as e:
            raise HTTPException(400, str(e)[:80])
        if p.ok and p.ticket is not None and b.get("source") == "callout":
            # the idea came from the analyst feed: say so in the audit, so callout-origin tickets can be measured apart (Round 10)
            g._audit("stage_source", ticket=p.ticket.hash8, origin="ui", code="callout", sha8=p.ticket.hash8)
        return {"ok": p.ok, "placeable": p.placeable, "reasons": p.reasons,
                "verdict": [{"check": n, "ok": ok, "detail": d} for n, ok, d in p.verdict],
                "sizing": p.sizing, "ticket": ticket_view(p.ticket, g) if p.ticket else None,
                "scale": ({"multiplier": scale["multiplier"], "reasons": scale["reasons"]} if scale else None)}

    @app.post("/api/place")
    async def api_place(request: Request):
        b = await body(request)
        g = request.app.state.guard
        r = await g.place(str(b.get("ticket_id", "")), origin="ui", confirm_hash=str(b.get("confirm_hash", "")))
        return {"ok": r.ok, "state": r.state, "acked": r.acked, "order_id": r.order_id,
                "error_code": r.error_code, "reasons": list(r.reasons),
                "note": "ACK means the broker accepted the order. It is not a fill." if r.acked else None}

    @app.post("/api/flatten")
    async def api_flatten(request: Request):
        b = await body(request)
        c = b.get("contract")
        if c is not None:
            contract_arg(c)
        r = await request.app.state.guard.flatten(contract_id=c, origin="ui")
        return {"flat": r.flat, "orphan_orders": r.orphan_orders, "reasons": list(r.reasons)}

    @app.post("/api/stop")
    async def api_stop(request: Request):
        """Tighten the working stop. Human click only: there is no MCP equivalent. Never widens."""
        b = await body(request)
        c = b.get("contract")
        contract_arg(c)
        try:
            price = float(b.get("stop_price"))
        except (TypeError, ValueError):
            raise HTTPException(400, "stop_price must be a number")
        r = await request.app.state.guard.move_stop(c, price, origin="ui")
        return {"ok": r.ok, "stop_price": r.stop_price, "reason": r.reason}

    @app.post("/api/halt")
    async def api_halt(request: Request):
        await body(request)
        request.app.state.guard.trip_halt("manual")
        return {"halted": True}

    @app.post("/api/halt/clear")
    async def api_halt_clear(request: Request):
        """Human-only. There is deliberately no MCP equivalent."""
        await body(request)
        request.app.state.guard.clear_halt()
        return {"halted": request.app.state.guard.halted()}

    @app.post("/api/reconcile")
    async def api_reconcile(request: Request):
        await body(request)
        return await request.app.state.guard.reconcile()

    @app.post("/api/intent/resolve")
    async def api_intent_resolve(request: Request):
        """Human-only. Closes intents that reconcile could not find at the broker, so the desk is not stuck forever
        after a send that never landed. No MCP equivalent."""
        await body(request)
        n = request.app.state.guard.resolve_not_found()
        return {"resolved": n, "unreconciled": request.app.state.guard.unreconciled()}

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    return app


SAFE_NAME = re.compile(r"^[A-Z0-9]{2,12}$")
SAFE_TEXT = re.compile(r"^[A-Za-z0-9 &./()\-]{1,60}$")


def _safe_name(v, contract_id):
    """A contract name like MESZ6 passes; anything else falls back to the root and expiry parsed from the id."""
    if isinstance(v, str) and SAFE_NAME.match(v):
        return v
    parts = str(contract_id).split(".")
    return "".join(parts[-2:]) if len(parts) >= 2 else "NA"


def _safe_text(v, fallback):
    """Gateway free text may only pass if it is short and punctuation-light; else use the fallback."""
    return v if isinstance(v, str) and SAFE_TEXT.match(v) else fallback


def _boot_code(e):
    """Fixed codes only: gateway error text never reaches the page or an AI reader."""
    if isinstance(e, PXNotConfigured):
        return "NOT_CONFIGURED"
    if isinstance(e, PXAuthError):
        return "AUTH_FAILED"
    if isinstance(e, PXRateLimited):
        return "RATE_LIMITED"
    if isinstance(e, PXError) and "no tradable account" in str(e):
        return "NO_ACCOUNT"
    return "UPSTREAM_ERROR"


async def _snapshot(app):
    """Read positions, orders and today's fills from REST and apply them without clobbering
    anything a live event changed while the reads were in flight."""
    c, live = app.state.client, app.state.live
    live.begin_seed()
    try:
        positions = await c.open_positions(live.account_id)
        orders = await c.open_orders(live.account_id)
        trades = await c.trades(live.account_id, session_start().isoformat(), datetime.now(timezone.utc).isoformat())
    except BaseException:
        live._touched = None
        raise
    live.seed(positions, orders)
    live.seed_trades(trades)
    guard = getattr(app.state, "guard", None)
    if guard is not None:       # a REST seed raises no position event: make sure nothing it found is naked
        try:
            await guard.protect_open_positions()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - recovery must never break the reseed
            print(f"protect failed: {type(e).__name__}", flush=True)


async def _boot(app):
    """Pick the account, seed from REST, then start the user hub."""
    c, live, user = app.state.client, app.state.live, app.state.user
    accts = await c.accounts(True)
    want = os.getenv("PX_ACCOUNT_ID")
    acct = next((a for a in accts if str(a["id"]) == want), None) if want else \
        next((a for a in accts if a.get("canTrade")), None)
    if acct is None:
        raise PXError("no tradable account found (set PX_ACCOUNT_ID)")
    bound = getattr(getattr(app.state, "guard", None), "limits", None)
    if bound is not None and not want:
        # Opens already refuse without a bound account, but flatten, the watchdog and PX_FLAT_BY act on whatever account is
        # selected: never let them run on an arbitrary one.
        raise PXError("PX_ACCOUNT_ID is not set: refusing to act on an arbitrary account")
    live.select_account(acct["id"])
    live.account = acct
    await user.subscribe("SubscribeAccounts", unsub_target="UnsubscribeAccounts")
    for t in ("Orders", "Positions", "Trades"):
        await user.subscribe(f"Subscribe{t}", acct["id"], unsub_target=f"Unsubscribe{t}")
    user.start()
    if not getattr(app.state, "reseed_listening", False):          # once, and BEFORE the snapshot: a failed first snapshot must still reseed on a resync
        app.state.reseed_listening = True
        user.listen(lambda kind: _schedule_reseed(app) if kind == "resync" else None)   # any user-hub reconnect may have missed events
    await _snapshot(app)


async def _boot_retry(app):
    """The first boot can fail (network not up when the service starts, a login hiccup). Keep trying on a backoff until it works."""
    delay = 5.0
    while app.state.boot_error:
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)
        try:
            await _boot(app)
            app.state.boot_error = None
            print("boot recovered", flush=True)
        except (PXError, KeyError, IndexError) as e:
            app.state.boot_error = _boot_code(e)


async def _deadline_loop(app):
    """Once a second: at PX_FLAT_BY, flatten. A failed flatten halts and is retried every 5 seconds while exposure remains."""
    while True:
        try:
            await asyncio.sleep(1.0)
            g = getattr(app.state, "guard", None)
            r = await g.enforce_flat_by() if g is not None else None
            if r is not None and not r.flat:
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the loop must survive anything
            print(f"deadline loop: {type(e).__name__}", flush=True)


async def _indicators_eval(app, cid, tf, res=None):
    """indicators.evaluate for one (contract, timeframe) from the cached respect scan and the bars it already fetched.
    None while the data is not there yet. Raises RespectError with a fixed code."""
    res = res if res is not None else await px_digest.respect_result(app, cid)
    d = ((res or {}).get("tfs") or {}).get(tf)
    hit = getattr(app.state, "respect_bars", {}).get((cid, tf))
    if not isinstance(d, dict) or not hit or d.get("note"):
        return None
    bars = px_respect.normalize(hit["bars"])
    if not bars:
        return None
    return px_ind.evaluate(d, bars)


TRACK_MIN_S = 60.0


def _track_interval():
    try:
        return max(TRACK_MIN_S, float(os.getenv("PX_TRACK_INTERVAL_S", 300)))
    except ValueError:
        return 300.0


async def _resolve_roots(app, roots, have):
    """Front contract id per root, added to `have` (root -> id). Unknown roots are skipped with a key-name-only warning."""
    now = datetime.now(timezone.utc)
    for root in roots:
        if root in have:
            continue
        try:
            cs = await app.state.client.search_contracts(root, live=_live_data())
        except PXError:
            raise
        cid = px_ind.front_contract(cs, root, now.year, now.month)
        if cid is None:
            print(f"track: no contract found for root {root}", flush=True)
            have[root] = None            # remembered so an unknown root is not searched every cycle
            continue
        have[root] = cid


async def _track_loop(app, roots):
    """Desk-side tracking, no browser needed: keep the trade tape on for the front contract of each root, rescan the
    respect set every PX_TRACK_INTERVAL_S (sequentially: retrieveBars has a budget), and append the grade log."""
    grade = px_grades.GradeLog.from_env(ROOT)
    app.state.grades = grade
    have, taped, delay = {}, set(), 5.0
    while app.state.boot_error:                       # hubs are not up until the boot succeeded
        await asyncio.sleep(2.0)
    while True:
        try:
            await _resolve_roots(app, roots, have)
            for cid in [c for c in have.values() if c and c not in taped]:
                await app.state.live._ensure_tape(cid)
                taped.add(cid)
            if app.state.market is not None and taped:
                app.state.market.start()
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - retry with backoff
            print(f"track: boot failed ({type(e).__name__}), retrying", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)
    interval = _track_interval()
    while True:
        for root in roots:
            cid = have.get(root)
            if not cid:
                continue
            try:
                res = await px_digest.respect_result(app, cid)
                for tf in px_respect.TIMEFRAMES:
                    ev = await _indicators_eval(app, cid, tf, res)
                    if ev is not None:
                        grade.log_eval(cid, tf, ev)
            except asyncio.CancelledError:
                raise
            except px_digest.RespectError as e:
                print(f"track: scan failed {root} {e.code}", flush=True)
            except Exception as e:  # noqa: BLE001 - one contract never stops the loop
                print(f"track: scan failed {root} {type(e).__name__}", flush=True)
            await asyncio.sleep(1.0)
        await asyncio.sleep(interval)


def _schedule_reseed(app):
    t = getattr(app.state, "reseed_task", None)
    if t is None or t.done():
        app.state.reseed_task = asyncio.get_running_loop().create_task(_reseed(app))


async def _reseed(app):
    """Re-read after a resync. On failure, mark state UNSEEDED (so day P&L reads None and the
    guard refuses to open) and retry with backoff until it works; never carry on silently with stale state."""
    live = app.state.live
    delay = 1.0
    while True:
        try:
            await _snapshot(app)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - any failure must flag state as unseeded
            live.positions_seeded = live.trades_seeded = False
            print(f"reseed failed: {type(e).__name__}", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


def main():
    import uvicorn
    from projectx_desk.env import load_env_file
    loaded = load_env_file()
    if loaded:
        print(f"loaded {len(loaded)} settings from .env.projectx (names only): {', '.join(sorted(loaded))}", flush=True)
    host = os.getenv("PX_HOST", "127.0.0.1")
    problem = px_net.bind_problem(host, os.getenv("PX_OPERATOR_TOKEN"), os.getenv("PX_ALLOWED_HOSTS"), px_mcp.weak_token_reason)
    if problem:
        raise SystemExit(problem)
    if not px_net.is_loopback(host):
        print(f"WARNING: bound to {host}. The operator login is HTTP Basic; without TLS in front it sends the token in the clear. "
              "Use Tailscale or a TLS reverse proxy (docs/DEPLOY.md).", flush=True)
    _lock = px_lock.acquire()           # noqa: F841 - held for the life of the process
    uvicorn.run(create_app(), host=host, port=_port(), log_level="info")


if __name__ == "__main__":
    main()
