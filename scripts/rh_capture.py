#!/usr/bin/env python
"""Save the Robinhood Trading MCP tool list (names, descriptions, input schemas) to a JSON file for drift checks.

Read-only: it calls `tools/list` and nothing else. It signs in through the desk's token file (`PX_RH_TOKEN_FILE`,
default data/rh_tokens.json); with no token it runs the same OAuth login the desk uses (browser on this machine, or the
`ssh -L` route in docs/ROBINHOOD.md for a headless host). The output holds NO account data: only tool names,
description text, and the structure of each input schema (property names, types, required lists, enum values), with
every default, example and const dropped.

    python scripts/rh_capture.py                          # writes data/rh_tools.json
    python scripts/rh_capture.py --out tools-new.json
    python scripts/rh_capture.py --url http://127.0.0.1:PORT/mcp --token-file data/rh_tokens.mock.json   # the dev mock
    python scripts/rh_drift.py tools-old.json tools-new.json
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk.rh import mcp, oauth, tokens  # noqa: E402

_DROP = {"default", "examples", "example", "const", "title", "$comment"}
_KEEP_STR = {"type", "description", "format", "pattern"}


def _schema(s, depth=0):
    """Structure only. Anything that could carry a value (default, example, const) is dropped."""
    if not isinstance(s, dict) or depth > 8:
        return {}
    out = {}
    for k, v in s.items():
        if k in _DROP:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {str(n): _schema(p, depth + 1) for n, p in v.items()}
        elif k == "required" and isinstance(v, list):
            out[k] = sorted(str(x) for x in v if isinstance(x, str))
        elif k == "enum" and isinstance(v, list):
            out[k] = [x for x in v if isinstance(x, (str, int, float, bool))]
        elif k in ("items", "additionalProperties") and isinstance(v, dict):
            out[k] = _schema(v, depth + 1)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            out[k] = [_schema(x, depth + 1) for x in v]
        elif k == "type" and isinstance(v, (str, list)):
            out[k] = v
        elif k in _KEEP_STR and isinstance(v, str):
            out[k] = v
    return out


def sanitize(tools):
    out = []
    for t in tools:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str):
            continue
        out.append({"name": t["name"], "description": t.get("description") if isinstance(t.get("description"), str) else "",
                    "input_schema": _schema(t.get("inputSchema") or t.get("input_schema") or {})})
    return sorted(out, key=lambda t: t["name"])


def write_atomic(path, payload):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        print(file=f)
    os.replace(tmp, p)


async def capture(url, store, transport=None):
    client = mcp.Client(url, store, transport=transport)
    return sanitize(await client.list_tools())


async def _login(url, store):
    import webbrowser
    lg = oauth.Login(url, store, opener=lambda u: webbrowser.open(u))
    info = await lg.start()
    print("Sign in here (opened in your browser if possible):", info["url"], file=sys.stderr)
    try:
        for _ in range(600):
            st = lg.status(info["pending_id"])
            if st != "pending":
                return st == "done"
            await asyncio.sleep(1)
        return False
    finally:
        await lg.aclose()


async def amain(args):
    url = args.url or os.environ.get("PX_RH_URL", "").strip() or mcp.ENDPOINT
    tf = args.token_file or os.environ.get("PX_RH_TOKEN_FILE", "").strip() or str(ROOT / "data" / "rh_tokens.json")
    store = tokens.TokenStore(tf, base_url=url)
    if store.load() is None and not await _login(url, store):
        print("login failed or expired", file=sys.stderr)
        return 2
    try:
        tools = await capture(url, store)
    except mcp.McpError as e:
        print(f"capture failed: {e.code}", file=sys.stderr)
        return 2
    write_atomic(args.out, {"captured_at": int(time.time()), "tools": tools})
    print(f"{len(tools)} tools -> {args.out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "data" / "rh_tools.json"))
    ap.add_argument("--url", default="")
    ap.add_argument("--token-file", default="")
    return asyncio.run(amain(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
