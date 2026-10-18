"""Analyst futures callout feed (read-only, advisory). Source: TraderDiscord-v2 `GET /analyst-callouts`.

The bot's callout text is untrusted third-party content from a premium room, so NOTHING free-form crosses
this module: no message text, no URLs. Rows are reduced to an allowlist of validated names, enums and
numbers. A callout is an analyst's opinion, not a signal, and has no path to the order guard.

Config (env, all optional; unset URL = feed off):
  PX_FEED_URL       full URL of /analyst-callouts. https only, or http to loopback.
  PX_FEED_KEY       the bot's X-Bot-Key. Never logged, never returned.
  PX_FEED_CHANNELS  comma-separated substrings of channel names to keep (default "futures").
  PX_FEED_HOURS     look-back window, 1..72 (default 24).
  PX_FEED_STAGE     1 shows a one-click STAGE button on a fresh ENTRY (read from Limits). Default 0: the banner and the
                    drawn levels still appear, but staging a callout takes the same typing as any other idea. The feed is
                    an unmeasured signal; the desk should not make it the lowest-friction entry on the screen (Round 10).
"""
import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

TTL_S = 20.0
MAX_ITEMS = 12
MAX_BODY = 1_000_000
SYMBOLS = frozenset("MNQ MES NQ ES MGC GC MCL CL YM MYM RTY M2K".split())
SIDES = frozenset(("LONG", "SHORT"))
EVENTS = frozenset("ENTRY ADD TRIM TP_HIT STOP_HIT CLOSE EVALUATION COMMENT".split())
_NAME_RE = re.compile(r"^@?[A-Za-z0-9_.\-]{1,32}$")
_CHAN_RE = re.compile(r"^[a-z0-9\-]{1,40}$")
PX_KEYS = ("entry", "stop", "tp1", "tp2", "tp3")
_SRC_PX = {"entry": "entry_px", "stop": "stop_px", "tp1": "tp1_px", "tp2": "tp2_px", "tp3": "tp3_px"}


class FeedError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fin(v):
    try:
        return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    except OverflowError:                 # an int too large for a float
        return False


def _price(v):
    if not _fin(v) or not 0 < v <= 10_000_000:
        return None
    r = round(float(v), 2)
    return r if r > 0 else None


def _points(v):
    if not _fin(v) or abs(v) > 5000:
        return None
    return round(float(v), 2)


def _parse_ts(s):
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def channel_tag(name):
    """`🔔╏futures-room` -> `futures-room`; anything that does not reduce to a plain slug -> None."""
    if not isinstance(name, str):
        return None
    tail = name.rsplit("╏", 1)[-1].strip().lower()
    return tail if _CHAN_RE.fullmatch(tail) else None


def sanitize_feed(rows, now=None, channels=("futures",)):
    """Raw API rows -> newest-first list of allowlisted items. Pure. Drops deleted rows, rows outside the
    channel filter, and rows carrying no structured information (a bare COMMENT with nothing parsed)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("deleted"):
            continue
        tag = channel_tag(r.get("channel_name"))
        if tag is None or not any(c in tag for c in channels):
            continue
        t = _parse_ts(r.get("posted_at"))
        if t is None:
            continue
        item = {
            "ts": t.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "age_s": max(0, int((now - t).total_seconds())),
            "channel": tag,
            "analyst": r["author_name"] if isinstance(r.get("author_name"), str) and _NAME_RE.fullmatch(r["author_name"]) else None,
            "symbol": r["symbol"] if isinstance(r.get("symbol"), str) and r["symbol"] in SYMBOLS else None,
            "side": r["side"] if isinstance(r.get("side"), str) and r["side"] in SIDES else None,
            "event": r["event"] if isinstance(r.get("event"), str) and r["event"] in EVENTS else "COMMENT",
            "points": _points(r.get("result_points")),
            "has_image": bool(r.get("has_image")),
        }
        for k, src in _SRC_PX.items():
            item[k] = _price(r.get(src))
        if item["event"] == "COMMENT" and item["points"] is None and item["symbol"] is None and not any(item[k] for k in PX_KEYS):
            continue
        out.append(item)
    out.sort(key=lambda i: i["ts"], reverse=True)
    return out[:MAX_ITEMS]


def feed_config(environ=None):
    """(url, key, channels, hours) or None when the feed is off. Raises FeedError('CONFIG') on an unsafe URL."""
    env = os.environ if environ is None else environ
    url = (env.get("PX_FEED_URL") or "").strip()
    if not url:
        return None
    u = urlparse(url)
    loop = u.hostname in ("127.0.0.1", "localhost", "::1")
    if not u.hostname or u.scheme not in ("https", "http") or (u.scheme == "http" and not loop):
        raise FeedError("CONFIG")                       # never send the key in cleartext off-box
    key = (env.get("PX_FEED_KEY") or "").strip()
    if not key:
        raise FeedError("CONFIG")
    chans = tuple(c.strip().lower() for c in (env.get("PX_FEED_CHANNELS") or "futures").split(",") if c.strip()) or ("futures",)
    try:
        hours = max(1, min(int(env.get("PX_FEED_HOURS") or 24), 72))
    except ValueError:
        hours = 24
    return url, key, chans, hours


async def fetch_rows(url, key, hours, now):
    since = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as c:
            async with c.stream("GET", url, params={"since": since, "limit": 500}, headers={"X-Bot-Key": key}) as r:
                if r.status_code in (401, 403):
                    raise FeedError("AUTH")
                if r.status_code != 200:
                    raise FeedError("UPSTREAM_ERROR")
                buf = bytearray()
                async for chunk in r.aiter_bytes():          # hard cap while reading (500 rows is well under 1 MB)
                    buf += chunk
                    if len(buf) > MAX_BODY:
                        raise FeedError("UPSTREAM_ERROR")
    except httpx.HTTPError:
        raise FeedError("UPSTREAM_ERROR") from None
    try:
        data = json.loads(bytes(buf))
    except (ValueError, RecursionError):
        raise FeedError("UPSTREAM_ERROR") from None
    if not isinstance(data, list):
        raise FeedError("UPSTREAM_ERROR")
    return data


async def feed_result(app, environ=None, fetch=fetch_rows):
    """Cached, single-flight. Always returns {v, status, asof, items}; status is OFF | OK | ERROR (+ code).
    Never raises: the feed is advisory and must not be able to break the desk."""
    st = app.state
    now = datetime.now(timezone.utc)
    lim = getattr(getattr(st, "guard", None), "limits", None)
    base = {"v": 1, "asof": now.isoformat(timespec="seconds"), "items": [],
            "stage": bool(getattr(lim, "feed_stage", False))}      # PX_FEED_STAGE=1: the desk may offer a one-click STAGE on an ENTRY
    try:
        cfg = feed_config(environ)
    except FeedError as e:
        return base | {"status": "ERROR", "code": e.code}
    if cfg is None:
        return base | {"status": "OFF"}
    url, key, chans, hours = cfg
    if not hasattr(st, "feed_lock"):
        st.feed_lock = asyncio.Lock()
        st.feed_cache = None
    async with st.feed_lock:
        hit = st.feed_cache
        if hit and time.monotonic() - hit[0] < TTL_S:
            return hit[1]
        try:
            rows = await fetch(url, key, hours, now)
            res = base | {"status": "OK", "items": sanitize_feed(rows, now, chans)}
        except FeedError as e:
            res = base | {"status": "ERROR", "code": e.code}
        except Exception:  # noqa: BLE001
            res = base | {"status": "ERROR", "code": "UPSTREAM_ERROR"}
        st.feed_cache = (time.monotonic(), res)
        return res


def feed_lines(f):
    """Digest lines. Fixed keys; absent = NA. No free text."""
    if not f or f.get("status") == "OFF":
        return ["CALLOUTS: OFF"]
    if f.get("status") != "OK":
        return [f"CALLOUTS: ERROR code={f.get('code', 'UPSTREAM_ERROR')}"]
    items = f["items"]
    if not items:
        return ["CALLOUTS: NONE (no structured callouts in window)"]
    L = [f"CALLOUTS: n={len(items)} newest_age_s={items[0]['age_s']}"]
    for n, i in enumerate(items[:6], 1):
        px = " ".join(f"{k}={i[k]}" for k in PX_KEYS if i[k] is not None)
        L.append(f"CALLOUT_{n}: age_s={i['age_s']} {i['channel']} analyst={i['analyst'] or 'NA'} sym={i['symbol'] or 'NA'} "
                 f"side={i['side'] or 'NA'} event={i['event']}" + (f" {px}" if px else "")
                 + (f" pts={i['points']}" if i["points"] is not None else ""))
    L.append(f"CALLOUTS_NOTE: third-party analyst callouts, opinion not signal; no order path; one_click_stage={'ON' if f.get('stage') else 'OFF'}")
    return L
