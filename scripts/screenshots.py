#!/usr/bin/env python
"""Regenerate the screenshots in docs/img from the OFFLINE MOCK desk. Nothing real is ever on screen.

It boots the mock gateway and a desk pointed at it (no .env.projectx, a fake account), plus two tiny local fake feeds (analyst callouts and
GEX), then drives the real UI with Playwright: an analyst ENTRY alert, STAGE, the hold-to-place gesture, the fill, the bracket tree, the
trade manager, stream mode and the AI panel. The mock market is random, so numbers differ a little on every run.

    pip install -r requirements-screenshots.txt && playwright install chromium
    python scripts/screenshots.py            # writes docs/img/*.png

Stream-safe by construction (AGENTS.md): it only talks to 127.0.0.1 ports it starts itself and refuses to run if `.env.projectx` values leak in.
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "img"
GW, DESK, FEED, GEXP = 8830, 8630, 8898, 8897
BASE = f"http://127.0.0.1:{DESK}"
MES, MNQ = "CON.F.US.MES.Z26", "CON.F.US.MNQ.Z26"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


def ask(cid):
    return get(f"/ai/state?contract={cid}")["quote"]["ask"]


class Shared:
    alert = False        # the callout feed stays empty until the script flips this
    mes = 5000.0         # the GEX feed follows the mock MES price
    mnq = 17500.0


def serve(port, handler):
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Callouts(BaseHTTPRequestHandler):
    posted = datetime.now(timezone.utc)

    def do_GET(self):
        rows = []
        if Shared.alert:
            e = Shared.mnq
            rows = [{"author_name": "@analyst", "channel_name": "futures-room", "posted_at": (Callouts.posted - timedelta(seconds=45)).isoformat(),
                     "symbol": "MNQ", "side": None, "event": "ENTRY", "entry_px": e, "stop_px": round(e + 8, 2), "tp1_px": round(e - 16, 2),
                     "tp2_px": None, "tp3_px": None, "result_points": None, "has_image": 0, "deleted": 0, "text": ""}]
        body = json.dumps(rows).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        shutil.copyfileobj(io.BytesIO(body), self.wfile)        # (the order-path pin forbids .write anywhere but the client)

    def log_message(self, *a):
        pass


class Gex(BaseHTTPRequestHandler):
    def do_GET(self):
        s = Shared.mes - 1.0       # SPX-like spot one point under the futures price (additive basis)
        lvl = lambda off, net, typ: {"strike": round(s + off, 2), "netGEX": net, "type": typ}
        data = {"symbol": "SPX", "spotPrice": s, "gammaFlipLevel": round(s + 4.0, 2), "maxGammaStrike": round(s - 1.5),
                "keyLevels": [lvl(-1.5, -2.9e9, "pin"), lvl(6.5, 1.2e9, "resistance"), lvl(-7.0, -8e8, "support")],
                "lastUpdated": datetime.now(timezone.utc).isoformat(), "interpretation": {"marketRegime": "Negative Gamma"}}
        body = json.dumps({"success": True, "data": data, "cached": False}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        shutil.copyfileobj(io.BytesIO(body), self.wfile)        # (the order-path pin forbids .write anywhere but the client)

    def log_message(self, *a):
        pass


def make_gif():
    """docs/img/demo.gif: a few of the screenshots in order, small enough for a README (no screen recording needed)."""
    from PIL import Image
    frames, names = [], ["desk-idle.png", "ui-analyst-alert.png", "ui-ticket-preview.png", "desk-in-trade.png", "ui-stream-mode.png"]
    for n in names:
        p = OUT / n
        if p.exists():
            im = Image.open(p).convert("RGB")
            if im.width > 1100:
                im = im.resize((1100, round(im.height * 1100 / im.width)))
            frames.append(im.quantize(colors=96, method=Image.Quantize.MEDIANCUT))
    if len(frames) > 1:
        w = max(f.width for f in frames); h = max(f.height for f in frames)
        frames = [f if f.size == (w, h) else f.crop((0, 0, w, h)) for f in frames]
        frames[0].save(OUT / "demo.gif", save_all=True, append_images=frames[1:], duration=2600, loop=0, optimize=True)


def main():
    from playwright.sync_api import sync_playwright
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pxshots-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PX_", "PROJECTX", "TD_"))}
    env.update({
        "PROJECTX_API": f"http://127.0.0.1:{GW}", "PROJECTX_HUB_BASE": f"ws://127.0.0.1:{GW}/hubs", "PROJECTX_USERNAME": "dev", "PROJECTX_API_KEY": "dev",
        "PX_ACCOUNT_ID": "4242", "PX_ACCOUNT_KIND": "sim", "PX_BRACKET_MODE": "position", "PX_DAILY_LOSS_LIMIT": "1000", "PX_DRAWDOWN_BUFFER": "2000",
        "PX_NO_NEW_AFTER": "16:55", "PX_TRADING": "1", "CHART_PORT": str(DESK), "PX_NO_ENV_FILE": "1", "PX_MCP_TOKEN": "",
        "PX_STATE_PATH": str(tmp / "day.json"), "PX_INTENT_PATH": str(tmp / "intents.json"), "PX_AUDIT_PATH": str(tmp / "audit.jsonl"),
        "PX_HALT_FILE": str(tmp / "HALT"), "PX_FLOW_FILE": str(tmp / "flow.json"), "PYTHONUNBUFFERED": "1",
        "PX_FEED_URL": f"http://127.0.0.1:{FEED}/analyst-callouts", "PX_FEED_KEY": "demo",
        "PX_GEX_URL": f"http://127.0.0.1:{GEXP}/api/v1", "PX_GEX_KEY": "demo", "DEV_GATEWAY_PORT": str(GW),
    })
    py = ROOT / ".venv" / "bin" / "python"
    py = str(py if py.exists() else sys.executable)
    procs = [subprocess.Popen([py, "-m", "projectx_desk.dev_gateway"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    srvs = [serve(FEED, Callouts), serve(GEXP, Gex)]
    try:
        time.sleep(3)
        procs.append(subprocess.Popen([py, "-m", "projectx_desk"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=2); break
            except Exception:
                time.sleep(0.5)
        Shared.mes, Shared.mnq = ask(MES), ask(MNQ)
        OUT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1680, "height": 1500})
            shot = lambda name, loc=None, **kw: (page.locator(loc).screenshot(path=str(OUT / name)) if loc else page.screenshot(path=str(OUT / name), **kw))
            page.goto(BASE + "/")
            page.wait_for_selector("#chart canvas", timeout=30000)
            page.click('#tfs button[data-v="minute,1"]')       # 1m bars so a few buy/sell volume bars exist within a few minutes
            time.sleep(int(os.getenv("PX_SHOTS_WARMUP", "200")))     # let the mock market print: tape bars, GEX, EMA stack
            Shared.mes = ask(MES)
            time.sleep(8)
            shot("desk-idle.png")
            shot("ui-size-box.png", "#size")
            shot("ui-chart-gex.png", "#chart")
            # the analyst posts an ENTRY: banner, chart switch, callout lines, STAGE button
            Shared.mnq = ask(MNQ)
            Shared.alert = True
            page.wait_for_selector("#callout-alert:not([hidden]) .ca-go", timeout=60000)
            time.sleep(1.5)
            shot("ui-analyst-alert.png")
            page.click(".ca-go")                             # STAGE: same preview path and checks as the normal button
            page.wait_for_selector("#tree:not([hidden])", timeout=15000)
            time.sleep(1)
            shot("ui-ticket-preview.png", "#ticket")
            btn = page.locator("#place-btn")                 # PLACE is a 600 ms hold, never a click
            btn.scroll_into_view_if_needed()
            time.sleep(2.5)                                  # the ticket must have been on screen a moment before the hold counts
            placed = False
            for how in ("mouse", "key"):
                if how == "mouse":
                    box = btn.bounding_box()
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.mouse.down(); time.sleep(1.0); page.mouse.up()
                else:
                    btn.focus(); page.keyboard.down("Space"); time.sleep(1.0); page.keyboard.up("Space")
                time.sleep(4)
                if page.locator("#tree-mode").inner_text().strip().upper() != "PREVIEW":
                    placed = True
                    break
            if not placed:
                print("PLACE did not complete:", page.evaluate("placeReady()"))
            time.sleep(6)                                    # fill, then the stop is sent and acknowledged
            shot("ui-bracket-tree.png", "#ticket")
            if page.locator("#manage:not([hidden])").count():
                shot("ui-manage-trade.png", "#manage")
            shot("desk-in-trade.png")
            page.click("#stream-toggle"); time.sleep(2.5)
            shot("ui-stream-mode.png")
            page.click("#stream-toggle")
            page.click("#ai-toggle"); time.sleep(2.5)
            page.evaluate("document.getElementById('ai-panel').style.cssText = 'flex:none;height:560px'")   # it is a flex child that collapses to its title bar
            page.locator("#ai-panel").scroll_into_view_if_needed(); time.sleep(1)
            shot("ui-ai-digest.png", "#ai-panel")
            page.set_viewport_size({"width": 430, "height": 900}); time.sleep(2)
            shot("ui-narrow.png", full_page=False)
            b.close()
        try:
            from PIL import Image
            for p in OUT.glob("*.png"):
                im = Image.open(p).convert("RGB")
                im.quantize(colors=200, method=Image.Quantize.MEDIANCUT).save(p, optimize=True)
        except ImportError:
            pass
        try:
            make_gif()
        except ImportError:
            pass
        print("wrote", sorted(p.name for p in OUT.glob("*.png")))
    finally:
        for s in srvs:
            s.shutdown()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
