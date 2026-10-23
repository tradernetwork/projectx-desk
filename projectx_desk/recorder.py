"""History import from a RECORDER (a log-only copy of the desk running on a server) into the trading desk's flow and tick bars.

The recorder (PX_RECORD_ONLY=1) has no order path; the trading desk stays on the trader's own device and pulls the
recorder's `/api/flow` and `/api/tickbars` read-only. Config (env, all optional; unset URL = off):
  PX_RECORDER_URL    base URL of the recorder. https only, or http to loopback (an ssh tunnel).
  PX_RECORDER_TOKEN  the recorder's PX_OPERATOR_TOKEN, sent as the HTTP Basic password. Never logged, never returned.

Same trust rules as feed.py: the recorder's JSON is untrusted. It is reduced to finite, range-checked numbers, bounded in size
and count, fetched with short timeouts, and failures surface as fixed codes, never remote text. The merge functions are pure.

Honesty rules for the merge (nothing is interpolated, summed across sources, or invented):
  flow   per minute bucket: the LOCAL bucket wins when it exists and is complete; else the recorder's bucket if it is complete;
         else the minute stays a partial hole. Local and recorder are never added together.
  ticks  the two sides cut bars at different prints, so bars are never mixed inside a stretch the local desk recorded. A
         recorder bar is added only if it is complete (not gap, full N prints) and its time span overlaps no local bar.
"""
import asyncio
import json
import math
import os
import time
from bisect import bisect_right
from urllib.parse import urlparse

import httpx

from projectx_desk import flow as px_flow
from projectx_desk import ticks as px_ticks

MIN_MS = px_flow.MIN_MS
TS_MIN, TS_MAX = px_flow.TS_MIN, px_flow.TS_MAX
MAX_CONTRACTS = 8
MAX_FLOW_ROWS = 2 * px_flow.SERVE_MINUTES          # buckets plus partial minutes that have no bucket
MAX_HOLES = px_flow.MAX_HOLES + 1                # the stored holes plus the one derived open hole
MAX_FLOW_BODY = 2_000_000
MAX_TICK_BODY = 4_000_000
MAX_VOL = 10**9
TTL_S = 600.0                                    # a recorder that stopped answering stops contributing after this
POLL_S = 60.0
TIMEOUT_S = 4.0


class RecorderError(Exception):
    """`code` is one of CONFIG AUTH TIMEOUT UPSTREAM_ERROR OVERSIZE BAD_DATA. Remote text never enters it."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---- config -----------------------------------------------------------------------------------

def recorder_config(environ=None):
    """(base_url, token) or None when off. Raises RecorderError('CONFIG') on an unsafe URL: the token never leaves in cleartext."""
    env = os.environ if environ is None else environ
    url = (env.get("PX_RECORDER_URL") or "").strip()
    if not url:
        return None
    try:
        u = urlparse(url)
        host = u.hostname
        u.port                                                  # a bad port raises ValueError here
    except ValueError:
        raise RecorderError("CONFIG") from None
    loop = host in ("127.0.0.1", "localhost", "::1")
    if not host or u.scheme not in ("https", "http") or (u.scheme == "http" and not loop) \
            or u.username or u.password or u.query or u.fragment:
        raise RecorderError("CONFIG")
    return url.rstrip("/"), (env.get("PX_RECORDER_TOKEN") or "").strip()


# ---- untrusted input -> allowlisted numbers ---------------------------------------------------

def _int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _fin(v):
    try:
        return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    except OverflowError:
        return False


def _minute(v):
    return _int(v, TS_MIN, TS_MAX) and v % MIN_MS == 0


def clean_flow(raw):
    """Recorder /api/flow body -> {"since","minutes","holes"} of validated ints, or RecorderError.
    A row that is malformed but names a valid minute becomes a PARTIAL row for that minute (never a quiet zero); a row that
    cannot even name its minute, a duplicate minute, or an oversize list rejects the whole body."""
    if not isinstance(raw, dict) or raw.get("v") != 1:
        raise RecorderError("BAD_DATA")
    mins, holes, since = raw.get("minutes"), raw.get("holes"), raw.get("since")
    if not isinstance(mins, list) or not isinstance(holes, list):
        raise RecorderError("BAD_DATA")
    if len(mins) > MAX_FLOW_ROWS or len(holes) > MAX_HOLES:
        raise RecorderError("OVERSIZE")
    if since is not None and not _minute(since):
        raise RecorderError("BAD_DATA")
    rows, seen = [], set()
    for r in mins:
        k = r[0] if isinstance(r, list) and r else None
        if not _minute(k) or k in seen:
            raise RecorderError("BAD_DATA")
        seen.add(k)
        ok = len(r) == 5 and all(_int(x, 0, MAX_VOL) for x in r[1:4]) and r[4] in (0, 1) and not isinstance(r[4], bool)
        rows.append([k, r[1], r[2], r[3], r[4]] if ok else [k, 0, 0, 0, 1])
    hs = []
    for h in holes:
        if not (isinstance(h, list) and len(h) == 2 and _minute(h[0]) and _minute(h[1]) and h[0] <= h[1]):
            raise RecorderError("BAD_DATA")
        hs.append([h[0], h[1]])
    rows.sort(key=lambda r: r[0])
    return {"since": since, "minutes": rows, "holes": hs}


def clean_ticks(raw, n):
    """Recorder /api/tickbars body -> {"since","bars":[row], "te":[ms]} of validated rows, or RecorderError. A malformed bar is
    simply not used (an unused bar can only leave the time to the local series, never invent data)."""
    if not isinstance(raw, dict) or raw.get("v") != 1 or raw.get("n") != n:
        raise RecorderError("BAD_DATA")
    bars, te, since = raw.get("bars"), raw.get("te"), raw.get("since")
    if not isinstance(bars, list) or not isinstance(te, list) or len(bars) != len(te):
        raise RecorderError("BAD_DATA")
    if len(bars) > px_ticks.MAX_BARS + 1:
        raise RecorderError("OVERSIZE")
    if since is not None and not _int(since, TS_MIN, TS_MAX):
        raise RecorderError("BAD_DATA")
    out, ends, last_t = [], [], 0
    for r, e in zip(bars, te):
        if not (isinstance(r, list) and len(r) == 10 and _int(r[0], TS_MIN, TS_MAX)) or not _int(e, r[0], TS_MAX):
            continue
        px, vols = r[1:5], r[5:8]
        if not all(_fin(x) and 0 < x <= px_ticks.MAX_PX for x in px) or not all(_fin(x) and 0 <= x <= MAX_VOL for x in vols):
            continue
        if not (px[2] <= px[0] <= px[1] and px[2] <= px[3] <= px[1]) or vols[1] + vols[2] > vols[0] + 1e-9:
            continue
        if r[8] not in (0, 1) or isinstance(r[8], bool) or not _int(r[9], 1, px_ticks.MAX_N) or r[0] <= last_t:
            continue
        last_t = r[0]
        out.append([r[0], px[0], px[1], px[2], px[3], vols[0], vols[1], vols[2], r[8], r[9]])
        ends.append(e)
    return {"since": since, "bars": out, "te": ends}


# ---- pure merges ------------------------------------------------------------------------------

def _merge_holes(h):
    out = []
    for a, b in sorted(h):
        if out and a <= out[-1][1] + MIN_MS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _side_holes(snap, asof_min=None):
    """Every stretch this side does NOT honestly cover: the stored holes, everything before `since`, and (for the recorder, which was
    read at one moment) everything from the minute it was read onward. Inclusive minute ranges, sorted and disjoint."""
    since = snap["since"]
    if since is None:
        return [[0, TS_MAX]]
    h = [list(x) for x in snap["holes"]] + [[0, since - MIN_MS]]
    if asof_min is not None:
        h.append([asof_min, TS_MAX])
    return _merge_holes(h)


def _intersect(a, b):
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo <= hi:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _in(k, holes, starts):
    i = bisect_right(starts, k) - 1
    return i >= 0 and k <= holes[i][1]


def merge_flow(local, remote, asof_ms):
    """(merged snapshot, used_remote). `local` is FlowStore.snapshot output, `remote` is clean_flow output, `asof_ms` is when the
    recorder was read (its forming minute, and anything after it, is not complete). Pure."""
    if remote is None or asof_ms is None or not _fin(asof_ms):
        return local, False
    asof_min = int(asof_ms) // MIN_MS * MIN_MS
    lh, rh = _side_holes(local), _side_holes(remote, asof_min)
    if local["since"] is None:
        lh = [[0, asof_min - MIN_MS]]               # a desk with no tape yet knows nothing up to the read time; the future is not a hole
    ls, rs = [h[0] for h in lh], [h[0] for h in rh]
    lrows = {r[0]: r for r in local["minutes"]}
    rrows = {r[0]: r for r in remote["minutes"]}
    rows, took = [], False
    for k in sorted(set(lrows) | set(rrows)):
        lr, rr = lrows.get(k), rrows.get(k)
        if lr is not None and not lr[4] and not _in(k, lh, ls):
            rows.append(list(lr))                                       # local complete: local wins
        elif rr is not None and not rr[4] and not _in(k, rh, rs):
            rows.append([k, rr[1], rr[2], rr[3], 0])                    # recorder complete, local is not: recorder fills it
            took = True
        elif lr is not None:
            rows.append([k, lr[1], lr[2], lr[3], 1])                    # neither complete: stays a partial hole
        elif _in(k, lh, ls):
            rows.append([k, 0, 0, 0, 1])                                # local was blind here and the recorder's minute is unusable: a marked hole
    sinces = [s for s in (local["since"], remote["since"]) if s is not None]
    since = min(sinces) if sinces else None
    if since is None:
        return local, False
    holes = [[max(a, since), b] for a, b in _intersect(lh, rh) if b >= since][-px_flow.MAX_HOLES:]
    used = took or since != local["since"] or holes != [list(h) for h in local["holes"]]
    if not used:
        return local, False
    return {"v": 1, "since": since, "minutes": rows, "holes": holes}, True


def merge_ticks(local, remote, n):
    """(merged snapshot, used_remote). `local` is TickBars.snapshot output (with `te`), `remote` is clean_ticks output. Pure."""
    lbars, lte = local.get("bars") or [], local.get("te")
    if remote is None or not remote["bars"] or (lbars and (not isinstance(lte, list) or len(lte) != len(lbars))):
        return local, False
    lte = lte or []
    starts = [r[0] for r in lbars]
    add = []
    for r, e in zip(remote["bars"], remote["te"]):
        if r[8] or r[9] != n:
            continue                                                    # gap-flagged or short (forming) bars are never imported
        i = bisect_right(starts, e) - 1
        if i >= 0 and lte[i] >= r[0]:
            continue                                                    # overlaps a local bar: the local desk's own bar stands
        add.append((r, e))
    if not add:
        return local, False
    allrows = sorted([(r, e) for r, e in zip(lbars, lte)] + add, key=lambda x: x[0][0])
    since = min(s for s in (local.get("since"), add[0][0][0]) if s is not None)
    return {"v": 1, "n": n, "since": since, "bars": [list(r) for r, _ in allrows], "te": [e for _, e in allrows]}, True


# ---- cache of what the recorder last said -----------------------------------------------------

class RecorderCache:
    """Holds the sanitized last answer per contract (and per tick size), expiring after TTL_S, plus a one-word `status`."""

    def __init__(self, status="pending"):
        self.status = status
        self.flow = {}          # cid -> (monotonic, asof_ms, clean)
        self.ticks = {}         # (cid, n) -> (monotonic, clean)

    def put_flow(self, cid, clean, asof_ms, now=None):
        if len(self.flow) >= MAX_CONTRACTS and cid not in self.flow:
            self.flow.pop(next(iter(self.flow)))
        self.flow[cid] = (time.monotonic() if now is None else now, asof_ms, clean)

    def put_ticks(self, cid, n, clean, now=None):
        if len(self.ticks) >= MAX_CONTRACTS * px_ticks.MAX_SIZES and (cid, n) not in self.ticks:
            self.ticks.pop(next(iter(self.ticks)))
        self.ticks[(cid, n)] = (time.monotonic() if now is None else now, clean)

    def merge_flow(self, cid, local, now=None):
        hit = self.flow.get(cid)
        if hit is None or (time.monotonic() if now is None else now) - hit[0] > TTL_S:
            return local, False
        return merge_flow(local, hit[2], hit[1])

    def merge_ticks(self, cid, n, local, now=None):
        hit = self.ticks.get((cid, n))
        if hit is None or (time.monotonic() if now is None else now) - hit[0] > TTL_S:
            return local, False
        return merge_ticks(local, hit[1], n)


# ---- fetch and poll (the only I/O) ------------------------------------------------------------

def _no_const(_):
    raise ValueError("non-finite")


async def fetch_json(base, path, params, token, max_body):
    try:
        auth = httpx.BasicAuth("desk", token) if token else None
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, auth=auth) as c:
            async with c.stream("GET", base + path, params=params) as r:
                if r.status_code in (401, 403):
                    raise RecorderError("AUTH")
                if r.status_code != 200:
                    raise RecorderError("UPSTREAM_ERROR")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_body:
                        raise RecorderError("OVERSIZE")
    except httpx.TimeoutException:
        raise RecorderError("TIMEOUT") from None
    except httpx.HTTPError:
        raise RecorderError("UPSTREAM_ERROR") from None
    try:
        return json.loads(bytes(buf), parse_constant=_no_const)
    except (ValueError, RecursionError):
        raise RecorderError("BAD_DATA") from None


async def poll_once(cache, cfg, cids, sizes, fetch=fetch_json):
    """Fetch flow and tick bars for each contract and cache the sanitized answers. Never raises. A failure leaves the cache as it
    was and sets `status` to `unavailable (CODE)`; a clean cycle sets it to `ok`."""
    base, token = cfg
    bad = None
    for cid in list(cids)[:MAX_CONTRACTS]:
        asof = int(time.time() * 1000)                  # taken BEFORE the request: the recorder's data reaches at least this far
        try:
            cache.put_flow(cid, clean_flow(await fetch(base, "/api/flow", {"contract": cid}, token, MAX_FLOW_BODY)), asof)
        except RecorderError as e:
            bad = bad or e.code
        except Exception:  # noqa: BLE001 - the recorder is advisory history; nothing it does may break the desk
            bad = bad or "UPSTREAM_ERROR"
        for n in sizes:
            try:
                raw = await fetch(base, "/api/tickbars", {"contract": cid, "n": n, "limit": px_ticks.MAX_BARS}, token, MAX_TICK_BODY)
                cache.put_ticks(cid, n, clean_ticks(raw, n))
            except RecorderError as e:
                bad = bad or e.code
            except Exception:  # noqa: BLE001
                bad = bad or "UPSTREAM_ERROR"
    cache.status = "ok" if bad is None else f"unavailable ({bad})"


async def poll_loop(cache, cfg, get_cids, get_sizes, fetch=fetch_json):
    """At start, then every POLL_S. Waits briefly while no tape is recorded yet."""
    while True:
        cids = get_cids()
        if cids:
            await poll_once(cache, cfg, cids, get_sizes(), fetch)
        await asyncio.sleep(POLL_S if cids else 5.0)
