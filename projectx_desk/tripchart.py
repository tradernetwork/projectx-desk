"""Trip chart: a PNG of what the chart looked like around one closed round trip, for the desk agent to Read.

Pillow is an OPTIONAL dependency (requirements-report.txt): without it `available()` is False and `render_png` returns False, and the
report still writes every number. The desk itself never imports this module; only scripts/scalp_report.py does. No I/O beyond saving
the one PNG it is asked to, no network, nothing from the guard, the client or the hubs.

Drawn: 1m candles for the window, EMA21 on the 1m (yellow) and on the 5m (blue, stepped, only ever the last CLOSED 5m bar), volume, the entry
and exit prices as dotted lines, a line from entry to exit coloured by outcome. Account ids, balances and tags are never drawn; the trip's net
dollars are, because the file lives in the gitignored data/ folder and exists to show the trader's own result.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .levels import ema_series

CT = ZoneInfo("America/Chicago")
W, H = 1100, 600
M_L, M_R, M_T, M_B = 14, 112, 64, 30
VOL_H = 80
PRE_MIN, POST_MIN, MIN_VISIBLE = 40, 8, 10
BG, GRID, TEXT = (17, 20, 24), (35, 40, 48), (207, 214, 224)
UP, DOWN, EMA1, EMA5 = (38, 166, 154), (239, 83, 80), (245, 197, 66), (74, 163, 255)


def available():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):          # an old Pillow without sized default fonts
        return ImageFont.load_default()


def _ema5_by_minute(bars_1m, bars_5m):
    """For every 1m bar, the 5m EMA21 as of that bar's close: the last 5m bar already closed by then, never a forming one."""
    e5 = ema_series([b["close"] for b in bars_5m], 21)
    out, j = [], -1
    for b in bars_1m:
        close = b["timestamp"] + 60_000
        while j + 1 < len(bars_5m) and bars_5m[j + 1]["timestamp"] + 300_000 <= close:
            j += 1
        out.append(e5[j] if j >= 0 else None)
    return out


def _money(v):
    return f"{'-' if v < 0 else '+'}${abs(v):.2f}"


def render_png(t, bars_1m, bars_5m, out_path, subtitle=""):
    """Draw the trip `t` (a journal round trip) over its bars and save to `out_path`. False when Pillow is missing or there are too few bars."""
    if not available() or t.get("open") or t.get("avg_entry") is None or t.get("avg_exit") is None:
        return False
    from PIL import Image, ImageDraw
    t0, t1 = t["entry_ts"] - PRE_MIN * 60_000, t["exit_ts"] + POST_MIN * 60_000
    idx = [i for i, b in enumerate(bars_1m) if t0 <= b["timestamp"] <= t1]
    if len(idx) < MIN_VISIBLE:
        return False
    e1 = ema_series([b["close"] for b in bars_1m], 21)
    e5 = _ema5_by_minute(bars_1m, bars_5m)
    vis = [bars_1m[i] for i in idx]
    ema_vals = [v for i in idx for v in (e1[i], e5[i]) if v is not None]
    lo = min([b["low"] for b in vis] + [t["avg_entry"], t["avg_exit"]])
    hi = max([b["high"] for b in vis] + [t["avg_entry"], t["avg_exit"]])
    span = max(hi - lo, 1e-9)
    near = [v for v in ema_vals if lo - span <= v <= hi + span]       # an EMA far off the candles must not flatten them
    lo, hi = min([lo] + near), max([hi] + near)
    pad = max((hi - lo) * 0.06, 1e-9)
    lo, hi = lo - pad, hi + pad
    pw, ph = W - M_L - M_R, H - M_T - M_B - VOL_H
    n = len(vis)
    cw = pw / n
    y = lambda p: M_T + (hi - p) / (hi - lo) * ph                                   # noqa: E731
    xc = lambda i: M_L + (i + 0.5) * cw                                              # noqa: E731
    vmax = max((b["volume"] for b in vis), default=0) or 1

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_s, f_m = _font(13), _font(16)
    for k in range(6):                                                               # price grid + right axis
        p = lo + (hi - lo) * k / 5
        d.line([(M_L, y(p)), (W - M_R, y(p))], fill=GRID)
        d.text((W - M_R + 6, y(p) - 7), f"{p:.2f}", fill=TEXT, font=f_s)
    step = max(1, n // 8)
    for i in range(0, n, step):                                                      # time axis (CT)
        d.text((xc(i) - 14, H - M_B + 6), f"{datetime.fromtimestamp(vis[i]['timestamp'] / 1000, CT):%H:%M}", fill=TEXT, font=f_s)
        d.line([(xc(i), M_T), (xc(i), M_T + ph + VOL_H)], fill=GRID)
    ent_i = next((i for i, b in enumerate(vis) if b["timestamp"] + 60_000 > t["entry_ts"]), 0)
    ext_i = next((i for i, b in enumerate(vis) if b["timestamp"] + 60_000 > t["exit_ts"]), n - 1)
    band = Image.new("RGBA", (W, H), (0, 0, 0, 0))                                   # the time the trade was open
    ImageDraw.Draw(band).rectangle([M_L + ent_i * cw, M_T, M_L + (ext_i + 1) * cw, M_T + ph + VOL_H],
                                   fill=(255, 255, 255, 18))
    img = Image.alpha_composite(img.convert("RGBA"), band).convert("RGB")
    d = ImageDraw.Draw(img)
    for i, b in enumerate(vis):                                                      # volume then candles
        col = UP if b["close"] >= b["open"] else DOWN
        vh = b["volume"] / vmax * (VOL_H - 6)
        d.rectangle([xc(i) - cw * 0.35, M_T + ph + VOL_H - vh, xc(i) + cw * 0.35, M_T + ph + VOL_H], fill=col)
        d.line([(xc(i), y(b["high"])), (xc(i), y(b["low"]))], fill=col)
        top, bot = y(max(b["open"], b["close"])), y(min(b["open"], b["close"]))
        d.rectangle([xc(i) - max(1.0, cw * 0.35), top, xc(i) + max(1.0, cw * 0.35), max(bot, top + 1)], fill=col)
    for series, col in ((e1, EMA1), (e5, EMA5)):
        pts = [(xc(k), y(series[i])) for k, i in enumerate(idx) if series[i] is not None]
        if len(pts) > 1:
            d.line(pts, fill=col, width=2)
    win = t["net"] > 0
    oc = UP if win else DOWN if t["net"] < 0 else TEXT
    for price, label in ((t["avg_entry"], "IN"), (t["avg_exit"], "OUT")):          # dotted price lines
        for x in range(int(M_L), int(W - M_R), 8):
            d.line([(x, y(price)), (x + 3, y(price))], fill=oc)
        d.text((W - M_R + 6, y(price) - 7), f"{label} {price:.2f}", fill=oc, font=f_s)
    d.line([(xc(ent_i), y(t["avg_entry"])), (xc(ext_i), y(t["avg_exit"]))], fill=oc, width=3)
    for (i, p) in ((ent_i, t["avg_entry"]), (ext_i, t["avg_exit"])):
        d.ellipse([xc(i) - 5, y(p) - 5, xc(i) + 5, y(p) + 5], fill=BG, outline=(255, 255, 255), width=2)
    root = t["contract"].split(".")[3] if t["contract"].count(".") >= 4 else t["contract"]
    held = f"{(t['hold_s'] or 0) // 60}m{(t['hold_s'] or 0) % 60:02d}s"
    d.text((M_L, 10), f"{root} {t['side']} x{t['qty']}   {datetime.fromtimestamp(t['entry_ts'] / 1000, CT):%Y-%m-%d %H:%M} CT   held {held}   net {_money(t['net'])}",
           fill=oc, font=f_m)
    d.text((M_L, 34), (subtitle + "   " if subtitle else "") + "1m candles | yellow EMA21 1m | blue EMA21 5m (last closed bar)", fill=TEXT, font=f_s)
    img.save(out_path, format="PNG", optimize=True)
    return True
