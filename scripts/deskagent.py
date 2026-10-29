#!/usr/bin/env python3
"""Desk agent: `claude -p` pointed at this checkout plus a read-only headless browser on the desk.

    scripts/deskagent.py ask "why is my order list empty?" [--session NAME]
    scripts/deskagent.py serve            # LAN page + /ask, bearer token in data/agent_token

Read-only by construction: Read/Grep/Glob, curl-free, and a Playwright MCP limited to navigate,
snapshot, screenshot, console and network. No Bash, Edit, Write, click, type or evaluate, so it cannot
press PLACE or reach an order route. Sessions resume per name (like disclaw threads).
Standalone tool: lives outside projectx_desk/ and never imports the desk.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TOKEN_FILE = DATA / "agent_token"
SESSIONS = DATA / "agent_sessions.json"
DESK = os.environ.get("PX_AGENT_DESK", "http://127.0.0.1:8600")
CHROME = os.environ.get("PX_AGENT_CHROME", str(Path.home() / ".cache/ms-playwright/chromium-1208/chrome-linux64/chrome"))
BROWSER = ["browser_navigate", "browser_snapshot", "browser_take_screenshot", "browser_console_messages",
           "browser_network_requests", "browser_wait_for", "browser_resize"]
ALLOWED = ["Read", "Grep", "Glob"] + ["mcp__deskbrowser__" + t for t in BROWSER]
# Belt and braces on top of dontAsk: nothing that writes or runs, and the files that hold secrets are unreadable to Read/Grep/Glob.
DENIED = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent",
          "Read(./.env*)", "Read(./data/agent_token)", "Read(./data/*.json)", "Read(./data/*.db*)",
          "Grep(./.env*)", "Grep(./data/agent_token)", "Glob(./.env*)"]
SYSTEM = (
    "You are the ProjectX Desk agent. Working directory is the desk checkout; read AGENTS.md first for the rules. "
    f"The live desk is {DESK} (open it in the deskbrowser tools to read the DOM/chart; /ai/digest and /state.json are "
    "plain GETs you can navigate to). You are read-only: you cannot place, cancel, flatten or click anything, and "
    "must never suggest bypassing a guard rule. Report facts from the page and the code, say what you could not see. "
    "Never print tokens or .env.projectx contents. Earlier turns of this conversation are stale: re-read the page or /ai/digest "
    "before stating anything about the current position, orders, price or halt. Terse, blunt, show the numbers."
)
LOCK = threading.Lock()


def mcp_config() -> str:
    return json.dumps({"mcpServers": {"deskbrowser": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.83", "--headless", "--isolated", "--executable-path", CHROME,
                 "--allowed-origins", DESK, "--viewport-size", "1600x1000"]}}})


def load_sessions() -> dict:
    try:
        return json.loads(SESSIONS.read_text())
    except (OSError, ValueError):
        return {}


def ask(prompt: str, session: str = "default", timeout: int = 280) -> str:   # under the desk relay's 300 s, so a 504 never leaves a run going
    with LOCK:  # one browser profile, one run at a time
        sessions = load_sessions()
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--append-system-prompt", SYSTEM,
               "--mcp-config", mcp_config(), "--strict-mcp-config", "--permission-mode", "dontAsk",
               "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED]
        if session in sessions:
            cmd += ["--resume", sessions[session]]
        try:
            run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
            out = json.loads(run.stdout)
        except subprocess.TimeoutExpired:
            return "timed out"
        except ValueError:
            return "agent failed: " + (run.stderr or run.stdout)[-400:]
        if out.get("session_id"):
            sessions[session] = out["session_id"]
            DATA.mkdir(exist_ok=True)
            SESSIONS.write_text(json.dumps(sessions))
        return str(out.get("result", ""))


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Desk agent</title>
<body style="font:15px system-ui;background:#111;color:#ddd;max-width:820px;margin:auto;padding:16px">
<textarea id=p rows=4 style="width:100%;background:#1b1b1b;color:#ddd;border:1px solid #333"></textarea>
<p><button id=go>Ask</button> session <input id=s value=default size=10> token <input id=t type=password size=12></p>
<pre id=o style="white-space:pre-wrap"></pre><script>
t.value=localStorage.px_agent_token||"";
go.onclick=async()=>{localStorage.px_agent_token=t.value;go.disabled=true;o.textContent="thinking...";
 try{const r=await fetch("/ask",{method:"POST",headers:{"Authorization":"Bearer "+t.value,"Content-Type":"application/json"},
 body:JSON.stringify({prompt:p.value,session:s.value})});o.textContent=r.ok?(await r.json()).result:"HTTP "+r.status}
 catch(e){o.textContent=String(e)}go.disabled=false}</script>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        shutil.copyfileobj(io.BytesIO(body), self.wfile)  # (the order-path pin forbids .write anywhere but the client)

    def do_GET(self) -> None:
        self._send(200, PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        token = TOKEN_FILE.read_text().strip()
        if self.path != "/ask" or self.headers.get("Authorization") != "Bearer " + token:
            return self._send(401, b'{"error":"no"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            result = ask(str(body["prompt"]), str(body.get("session") or "default"))
        except (ValueError, KeyError):
            return self._send(400, b'{"error":"bad request"}')
        self._send(200, json.dumps({"result": result}).encode())

    def log_message(self, *a) -> None:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("prompt")
    a.add_argument("--session", default="default")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=9315)
    args = ap.parse_args()
    if args.cmd == "ask":
        print(ask(args.prompt, args.session))
        return 0
    if not TOKEN_FILE.exists() or len(TOKEN_FILE.read_text().strip()) < 32:
        print("missing data/agent_token (openssl rand -hex 32 > data/agent_token; chmod 600)", file=sys.stderr)
        return 1
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
