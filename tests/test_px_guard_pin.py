"""Mechanical pin for the rule in CLAUDE.md: px_guard.py is the ONLY module that can move money.

Matches attribute ACCESS, not just calls (the live path passes bound methods, e.g.
`asyncio.to_thread(guard.place, ...)`), plus dynamic dispatch by string and the endpoint literals.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
WRITE_ENDPOINT = re.compile(r"(Order/(place|modify|cancel)|Position/(close|partialClose))", re.I)
GUARD_ONLY_NAMES = {"ORDER_PLACE", "ORDER_MODIFY", "ORDER_CANCEL", "POSITION_CLOSE", "POSITION_PARTIAL_CLOSE"}
# attributes that reach the order path; only the web layer may touch them (UI buttons), never MCP/state/etc.
MONEY_ATTRS = {"place", "flatten", "cancel_order", "close_position", "move_stop"}
MONEY_ALLOWED_FILES = {"px_app.py"}           # px_guard.py itself is exempt from every rule
WRITE_ALLOWED_FILES = {"px_client.py"}        # defines PXClient.write; may not CALL it
# Server-side mocks SERVE the endpoints and never call them. Exempt by name, and test_mock_is_server_only
# proves they import no client code, so the exemption cannot quietly become a second order path.
SERVER_SIDE_MOCKS = {"dev_gateway.py"}


def violations(source: str, filename: str):
    if filename == "px_guard.py" or filename in SERVER_SIDE_MOCKS:
        return []
    out, tree = [], ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and WRITE_ENDPOINT.search(node.value):
            out.append(f"{filename}:{node.lineno} endpoint literal {node.value!r}")
        if isinstance(node, ast.Name) and node.id in GUARD_ONLY_NAMES:
            out.append(f"{filename}:{node.lineno} uses guard-only constant {node.id}")
        if isinstance(node, ast.alias) and node.name in GUARD_ONLY_NAMES:
            out.append(f"{filename} imports guard-only constant {node.name}")
        if isinstance(node, ast.Attribute):
            if node.attr in MONEY_ATTRS and filename not in MONEY_ALLOWED_FILES:
                out.append(f"{filename}:{node.lineno} touches .{node.attr}")
            if node.attr == "write" and filename not in WRITE_ALLOWED_FILES:
                recv = ast.unparse(node.value).lower()
                if "client" in recv or recv in ("c", "gw", "self.client"):
                    out.append(f"{filename}:{node.lineno} calls {recv}.write")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr"):
            for a in node.args[1:2]:
                if isinstance(a, ast.Constant) and a.value in MONEY_ATTRS | {"write"} and filename not in MONEY_ALLOWED_FILES:
                    out.append(f"{filename}:{node.lineno} dynamic dispatch to {a.value!r}")
    return out


def test_no_module_but_the_guard_can_reach_the_order_path():
    bad = []
    for f in sorted(ROOT.glob("*.py")):
        bad += violations(f.read_text(), f.name)
    assert not bad, "\n".join(bad)


def test_pin_scans_the_modules_it_should():
    names = {f.name for f in ROOT.glob("*.py")}
    assert {"px_guard.py", "px_app.py", "px_client.py", "px_state.py", "px_hub.py"} <= names


def test_detector_catches_the_indirect_shapes():
    cases = {
        "endpoint literal": 'URL = "https://x/api/Order/place"',
        "case-insensitive literal": 'p = "/api/order/CANCEL"',
        "position close": 'p = "/api/Position/closeContract"',
        "bound method passed on": "import asyncio\nasyncio.to_thread(guard.place, tid)",
        "dotted access": "somepackage.module.guard.place",
        "getattr by string": 'getattr(g, "place")',
        "client write call": "await self.client.write(p, b)",
        "bare client write": "await c.write(p, b)",
        "guard constant by name": "x = ORDER_PLACE",
        "guard constant import": "from px_guard import ORDER_PLACE",
        "flatten from mcp": "await guard.flatten()",
    }
    for label, src in cases.items():
        assert violations(src, "px_mcp.py"), f"detector missed: {label}"


def test_detector_allows_what_it_should():
    assert violations("await guard.place(tid, origin='ui', confirm_hash=h)", "px_app.py") == []
    assert violations("p = await guard.preview(contract_id=c)", "px_mcp.py") == []
    assert violations('rows = await client.post("/api/Order/searchOpen", {})', "px_state.py") == []
    assert violations("self._http.post(url)", "px_client.py") == []
    assert violations('x = "/api/Order/place"', "px_guard.py") == []


def test_mock_is_server_only_so_its_exemption_is_safe():
    for name in SERVER_SIDE_MOCKS:
        tree = ast.parse((ROOT / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any(m.startswith("px_") or m in ("httpx", "requests", "urllib.request") for m in mods), (name, mods)
            if isinstance(node, ast.Name):
                assert node.id not in ("PXClient", "ClientSession"), (name, node.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("ws_connect",), (name, node.attr)
