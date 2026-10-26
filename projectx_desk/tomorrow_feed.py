"""Tomorrow's Map for the chart's contract: fetch the source symbol's gamma (TraderDaddy, the same key and base URL as gex.py), build the
map (tomorrow.py) and convert it to futures space. Display only: nothing here is read by the guard or by sizing, and a failure of any
kind returns a fixed status code, never an exception and never vendor text.

Source symbol per contract root is the ETF proxy the author's map was built on (MES -> SPY, MNQ -> QQQ, MGC -> GLD). The ratio that maps
source price to futures price is the futures close of the 5m bar that contained the snapshot, over the snapshot's spot: a ratio taken
against the LIVE last would soak up every point price has moved since the snapshot (the same trap gex._basis_ref documents).

`today` is the New York date: expirations dated after it survive tonight, today's die (0DTE is not in the ladder)."""
import asyncio
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from projectx_desk import gex, gex_futures, sizing
from projectx_desk import tomorrow as T
from projectx_desk.client import live_data
from projectx_desk.tradeday import CT

NY = ZoneInfo("America/New_York")
TTL_S = 600.0
ERR_TTL_S = 60.0
MAX_ROWS, MAX_EXPS, MAX_HIST = 600, 60, 6000
MAX_AGE_S = 5 * 86400                      # a map older than a trading week is not "tomorrow's"; younger ones show their age
RATIO_DEV = 0.35                           # the same gross sanity bound gex_futures puts on an ETF ratio
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HIST_PATH = "/historical?hours=168"


def map_source(root):
    """(source symbol, nominal ratio) for a futures root, or None when it has no ETF proxy the map supports."""
    r = gex_futures.resolve_root(root)
    row = gex_futures.MAPPING.get(r) if r else None
    if not row or row.get("supported", True) is False:
        return None
    for s in row["sources"]:
        if s["kind"] == gex_futures.ETF and s.get("in_source"):
            return s["symbol"], s.get("nominal_ratio")
    return None


async def fetch_json(base, key, path):
    """Parsed `data` of one TraderDaddy GET, size-capped while reading. Raises gex.GexError with a fixed code only."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as c:
            async with c.stream("GET", f"{base}{path}", headers={"X-API-Key": key, "Authorization": f"Bearer {key}"}) as r:
                if r.status_code in (401, 403):
                    raise gex.GexError("AUTH")
                if r.status_code == 429:                  # the API allows 30 requests a minute; each map costs three, cached ten minutes
                    raise gex.GexError("RATE_LIMIT")
                if r.status_code != 200:
                    raise gex.GexError("UPSTREAM_ERROR")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > gex.MAX_BODY:
                        raise gex.GexError("UPSTREAM_ERROR")
    except httpx.HTTPError:
        raise gex.GexError("UPSTREAM_ERROR") from None
    try:
        data = json.loads(bytes(buf))
    except (ValueError, RecursionError):
        raise gex.GexError("UPSTREAM_ERROR") from None
    if isinstance(data, dict) and data.get("success") is False:
        raise gex.GexError("UPSTREAM_ERROR")
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    return data


def clean_inputs(g, m, h):
    """Raw API payloads -> validated numbers only, or raises gex.GexError('BAD_PAYLOAD'). Vendor strings never survive."""
    num = gex._num
    if not (isinstance(g, dict) and isinstance(m, dict) and isinstance(h, list)):
        raise gex.GexError("BAD_PAYLOAD")
    spot = num(g.get("spotPrice"), 0, 1e7)
    net = num(g.get("totalGEX"), -1e15, 1e15)
    flip = num(g.get("gammaFlipLevel"), 0, 1e7)
    snap = gex._parse_epoch(m.get("snapshotTime")) or gex._parse_epoch(g.get("lastUpdated"))
    exps = m.get("expirations")
    rows = m.get("rows")
    if spot is None or net is None or snap is None or not isinstance(exps, list) or not isinstance(rows, list):
        raise gex.GexError("BAD_PAYLOAD")
    exps = [e for e in exps[:MAX_EXPS] if isinstance(e, str) and _DATE.match(e)]
    if len(exps) != len(m["expirations"][:MAX_EXPS]):
        raise gex.GexError("BAD_PAYLOAD")
    clean_rows = []
    for r in rows[:MAX_ROWS]:
        k = num(r.get("strike"), 0, 1e7) if isinstance(r, dict) else None
        vals = r.get("gex") if isinstance(r, dict) else None
        if k is None or abs(k / spot - 1.0) > 0.5 or not isinstance(vals, list) or len(vals) != len(exps):
            continue
        clean_rows.append({"strike": k, "gex": [num(v, -1e15, 1e15) for v in vals]})
    hist = [{"snapshotTime": p["snapshotTime"], "spotPrice": p["spotPrice"]} for p in h[:MAX_HIST]
            if isinstance(p, dict) and isinstance(p.get("snapshotTime"), str) and len(p["snapshotTime"]) <= 40 and num(p.get("spotPrice"), 0, 1e7) is not None]
    if len(clean_rows) < 3 or not hist:
        raise gex.GexError("BAD_PAYLOAD")
    return {"spot": spot, "net": net, "flip": flip, "snap": snap, "exps": exps, "rows": clean_rows, "hist": hist}


def build_from_clean(c, today, ratio, tick):
    """The futures-space map from validated inputs. None when the builder declines (thin ladder, no measured regime)."""
    ladder, share = T.survivor_ladder({"expirations": c["exps"], "rows": c["rows"]}, today)
    return T.build_tomorrow_map(c["spot"], c["flip"], c["net"], ladder, T.bars_from_history(c["hist"]), share, ratio=ratio, tick=tick)


def ref_epoch(snap_epoch):
    """The moment whose futures price pairs with the source's spot. During the cash session that is the snapshot itself. After the cash
    close (15:00 CT), overnight, or on a weekend, the ETF spot is frozen at its 15:00 CT close while the futures keep trading, so scaling
    by the snapshot's own futures bar would fold the overnight move into the ratio: use the last cash close (15:00 CT) instead, the
    previous weekday's when the snapshot is before the 08:30 open. Holidays are not modelled: no bar there is NO_REF, never a guess."""
    t = datetime.fromtimestamp(snap_epoch, CT)
    hm = (t.hour, t.minute)
    if t.weekday() < 5 and (8, 30) <= hm < (15, 0):
        return snap_epoch
    d = t.date()
    if not (t.weekday() < 5 and hm >= (15, 0)):          # before the open, or a weekend: the last close is a day or more back
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, 14, 59, 59, tzinfo=CT).timestamp()          # inside the 14:55 bar, whose close is the 15:00 price


async def futures_ref(app, contract_id, snap_epoch):
    """Close of the futures 5m bar that contained the snapshot, or None. One REST read, cached with the result. The bar may still be
    forming (a snapshot seconds old), in which case its close is the price now: within one bar of the snapshot, which is all a ratio needs."""
    t0 = datetime.fromtimestamp(snap_epoch, timezone.utc)
    raw = await app.state.client.bars({"contractId": contract_id, "live": live_data(), "unit": 2, "unitNumber": 5, "limit": 12,
                                       "startTime": (t0 - timedelta(minutes=30)).isoformat(), "endTime": (t0 + timedelta(minutes=10)).isoformat(),
                                       "includePartialBar": True})      # a snapshot taken seconds ago sits in the bar still forming
    best = None
    for b in raw or []:
        try:
            ts = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).timestamp()
            close = float(b["c"])
        except (KeyError, ValueError, TypeError):
            continue
        if math.isfinite(close) and close > 0 and ts <= snap_epoch < ts + 300:
            best = close
    return best


def _res(status, code=None, **kw):
    return {"v": 1, "status": status, "code": code, "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"), "map": None,
            "disclaimer": gex_futures.DISCLAIMER} | kw


async def tomorrow_result(app, contract_id, environ=None, fetch=fetch_json, now=None):
    """Always a dict: status OK | UNAVAILABLE | NO_KEY | ERROR (+ a fixed `code`). Never raises."""
    try:
        return await _tomorrow(app, contract_id, environ, fetch, now)
    except Exception:  # noqa: BLE001 - advisory
        return _res("ERROR", "INTERNAL")


async def _tomorrow(app, contract_id, environ, fetch, now):
    st = app.state
    if not hasattr(st, "tmap_lock"):
        st.tmap_lock = asyncio.Lock()
        st.tmap_cache = {}

    def remember(res):                          # an early refusal is cached too, so the AI surface reports the real code instead of LOADING forever
        if len(st.tmap_cache) >= 16:
            st.tmap_cache.pop(next(iter(st.tmap_cache)))
        st.tmap_cache[contract_id] = (time.monotonic(), res)
        return res
    try:
        cfg = gex.gex_config(environ)
    except gex.GexError as e:
        return remember(_res("ERROR", e.code))
    if cfg is None:
        return remember(_res("NO_KEY"))
    base, key, _ = cfg
    try:
        spec = sizing.spec_for(contract_id)
    except sizing.SizingError:
        return remember(_res("UNAVAILABLE", "CONTRACT"))
    src = map_source(spec.root)
    if src is None:
        return remember(_res("UNAVAILABLE", "UNSUPPORTED"))
    sym, nominal = src
    now_ts = time.time() if now is None else now
    async with st.tmap_lock:                                   # single flight: one fetch (and one bars read) per contract per TTL
        hit = st.tmap_cache.get(contract_id)
        if hit and time.monotonic() - hit[0] < (TTL_S if hit[1].get("status") == "OK" else ERR_TTL_S):
            return hit[1]
        return remember(await _compute(app, contract_id, spec, sym, nominal, base, key, fetch, now_ts))


async def _compute(app, contract_id, spec, sym, nominal, base, key, fetch, now_ts):
    if sym not in gex._SYMS:
        return _res("UNAVAILABLE", "UNSUPPORTED")
    try:
        g, m, h = await asyncio.gather(fetch(base, key, f"/gex/{sym}"), fetch(base, key, f"/gex/{sym}/matrix"), fetch(base, key, f"/gex/{sym}{HIST_PATH}"))
        c = clean_inputs(g, m, h)
    except gex.GexError as e:
        return _res("ERROR", e.code)
    age = now_ts - c["snap"]
    if age < -60:
        return _res("ERROR", "BAD_PAYLOAD")
    if age > MAX_AGE_S:
        return _res("UNAVAILABLE", "STALE")
    try:
        ref = await futures_ref(app, contract_id, ref_epoch(c["snap"]))
    except Exception:  # noqa: BLE001
        ref = None
    if ref is None:
        return _res("UNAVAILABLE", "NO_REF")
    ratio = ref / c["spot"]
    if nominal and abs(ratio / nominal - 1.0) > RATIO_DEV:
        return _res("UNAVAILABLE", "CONVERT")
    today = datetime.fromtimestamp(c["snap"], NY).date().isoformat()          # the snapshot's own date: a late retry must not drop tomorrow's expiry from "survives tonight"
    snap_ct = datetime.fromtimestamp(c["snap"], CT)
    after_hours = snap_ct.weekday() >= 5 or not ((8, 25) <= (snap_ct.hour, snap_ct.minute) <= (15, 5))
    mp = build_from_clean(c, today, ratio, spec.tick_size)
    if mp is None:
        return _res("UNAVAILABLE", "NO_MAP")
    return _res("OK", None, source=sym, root=spec.root, ratio=round(ratio, 6), tick=spec.tick_size, snapshot_epoch=int(c["snap"]),
                age_s=int(max(0.0, age)), today=today, after_hours=after_hours, source_spot=c["spot"], source_flip=c["flip"], map=mp)


MIN_SESSION_BARS = 60                      # 78 five-minute bars in 08:30 to 15:00; fewer means the session is not all there yet


async def session_bar(app, contract_id, session_date):
    """Regular-session {o, h, l, c} of the futures contract for a CT date (08:30 to 15:00 CT), or None until all of it is readable."""
    open_ = datetime(session_date.year, session_date.month, session_date.day, 8, 30, tzinfo=CT)
    close = open_.replace(hour=15, minute=0)
    raw = await app.state.client.bars({"contractId": contract_id, "live": live_data(), "unit": 2, "unitNumber": 5, "limit": 100,
                                       "startTime": open_.astimezone(timezone.utc).isoformat(), "endTime": close.astimezone(timezone.utc).isoformat(),
                                       "includePartialBar": False})
    rows = []
    for b in raw or []:
        try:
            ts = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).timestamp()
            o, h, l, c = (float(b[k]) for k in ("o", "h", "l", "c"))
        except (KeyError, ValueError, TypeError):
            continue
        if all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) and open_.timestamp() <= ts < close.timestamp():
            rows.append((ts, o, h, l, c))
    if len(rows) < MIN_SESSION_BARS:
        return None
    rows.sort()
    return {"o": rows[0][1], "h": max(r[2] for r in rows), "l": min(r[3] for r in rows), "c": rows[-1][4]}


async def ai_block(app, contract_id):
    """The compact map for /ai/state and the digest: ours alone (numbers, enums, our own sentences). Never blocks the state: it reads the
    cache, or starts one background load and says LOADING until it lands (the desk's own /api/tomorrow call usually got there first)."""
    st = app.state
    hit = getattr(st, "tmap_cache", {}).get(contract_id)
    if hit and time.monotonic() - hit[0] < (TTL_S if hit[1].get("status") == "OK" else ERR_TTL_S):
        res = hit[1]
    else:
        kicks = getattr(st, "tmap_kicks", None)
        if kicks is None:
            kicks = st.tmap_kicks = set()
        if contract_id not in kicks:
            kicks.add(contract_id)

            async def go():
                try:
                    await tomorrow_result(app, contract_id)
                finally:
                    kicks.discard(contract_id)
            tasks = getattr(st, "tmap_kick_tasks", None)
            if tasks is None:
                tasks = st.tmap_kick_tasks = set()
            t = asyncio.create_task(go())
            tasks.add(t)                                     # the loop holds tasks weakly: keep a reference until each one is done
            t.add_done_callback(tasks.discard)
            st.tmap_kick_task = t
        return {"status": "UNAVAILABLE", "code": "LOADING"}
    m = res.get("map")
    if res.get("status") != "OK" or not isinstance(m, dict):
        return {"status": res.get("status"), "code": res.get("code")}
    return {"status": "OK", "code": None, "source": res.get("source"), "root": res.get("root"), "age_s": res.get("age_s"), "regime": m["regime"],
            "spot": m["spot"], "flip": m["flip"], "pin": m["pin"], "typical_move": m["typical_move"], "expiring_share": m["expiring_share"],
            "regime_includes_expiring": m["regime_includes_expiring"], "thin_book": m["thin_book"], "flip_on_price": m["flip_on_price"],
            "roads": [{"key": r["key"], "cond": r["cond"], "rule": r["rule"], "plain": r.get("plain"), "band": r.get("band"),
                       "waypoints": [{"price": w["price"], "tag": w["tag"], "tail": w["tail"], "open": bool(w.get("open"))} for w in r["waypoints"]]}
                      for r in m["roads"]],
            "record": None}


def tomorrow_lines(t):
    """Digest lines for the AI surface. Positioning, not targets; never a rate until the record is publishable."""
    if not isinstance(t, dict):
        return []
    if t.get("status") != "OK":
        return [f"MAP: NA ({t.get('code') or t.get('status') or 'NA'})"]
    age = t.get("age_s") or 0
    out = [f"MAP: {t['source']}->{t['root']} age={age // 60}min regime={t['regime'].upper()} spot={t['spot']:g} flip={'NA' if t['flip'] is None else format(t['flip'], 'g')} "
           f"pin={'NA' if t['pin'] is None else format(t['pin'], 'g')} typical_move={t['typical_move']:g}pt"
           + (" thin_book=YES" if t.get("thin_book") else "")
           + (f" regime_counts_expiring_gamma={t['expiring_share']:g}" if t.get("regime_includes_expiring") and t.get("expiring_share") is not None else "")]
    verbs = {"UP": ("IF", "it goes"), "DOWN": ("IF", "it goes"), "RANGE": ("ELSE", "it holds")}
    for r in t["roads"]:
        kind, verb = verbs[r["key"]]
        out.append(f"MAP_{r['key'] if r['key'] != 'RANGE' else 'ELSE'}: {kind} {verb} {r['cond']} -> {r['rule']}")
        if r["waypoints"]:
            out.append(f"MAP_{r['key'] if r['key'] != 'RANGE' else 'ELSE'}_STOPS: " + "; ".join(
                w["tag"] + (" (tail)" if w["tail"] else "") + (" (thin)" if w["open"] else "") for w in r["waypoints"]))
    rec = t.get("record")
    if isinstance(rec, dict) and isinstance(rec.get("record"), dict):
        r = rec["record"]
        if r["publishable"]:
            out.append(f"MAP_RECORD: {r['sessions']} sessions graded; levels held {r['hits']}/{r['total']} vs round numbers {r['null_hits']}/{r['null_total']}")
        else:
            out.append(f"MAP_RECORD: {r['sessions']} of {r['min_sessions']} sessions graded (no rate shown until then)")
        lc = rec.get("last_call")
        if lc:
            out.append(f"MAP_LAST_CALL: {lc['session']} {lc['branch']} {lc['hits']}/{lc['total']} tested: {lc['summary']}")
    out.append("MAP_NOTE: positioning not targets; the map has no proven edge")
    return out
