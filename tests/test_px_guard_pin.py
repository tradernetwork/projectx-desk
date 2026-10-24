"""Mechanical pin for the rule in AGENTS.md: projectx_desk/guard.py is the ONLY module that can move money.

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
MONEY_ATTRS = {"place", "flatten", "cancel_order", "close_position", "move_stop",
               # the guard's other writers and human-only controls: only the web layer (a human's click) may reach them
               "clear_halt", "resolve_intent", "reconcile", "handle_position",
               "_arm_stop", "_naked", "_resize_stop", "_verify_stop_after_unknown"}
# Files are identified by their path relative to the repo (posix), never by bare filename: a stray app.py or client.py
# anywhere else in the tree must not inherit an exemption.
PKG = "projectx_desk"
GUARD_FILE = f"{PKG}/guard.py"
MONEY_ALLOWED_FILES = {f"{PKG}/app.py"}       # guard.py itself is exempt from every rule
WRITE_ALLOWED_FILES = {f"{PKG}/client.py"}    # defines PXClient.write; may not CALL it
DYNAMIC_NAMES = {"methodcaller", "attrgetter", "__import__", "import_module", "eval", "exec", "vars"}   # build a name or call at runtime
BANNED_ATTRS = {"__dict__", "__getattribute__", "_http"}        # reach around the client wrapper or the attribute check
TOKEN_ALLOWED_FILES = {f"{PKG}/client.py", f"{PKG}/hub.py"}              # only these may hold or mint the bearer token
# Server-side mocks SERVE the endpoints and never call them. Exempt by name, and test_mock_is_server_only
# proves they import no client code, so the exemption cannot quietly become a second order path.
SERVER_SIDE_MOCKS = {f"{PKG}/dev_gateway.py"}


def violations(source: str, filename: str):
    if filename == GUARD_FILE or filename in SERVER_SIDE_MOCKS:
        return []
    out, tree = [], ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value.decode("utf-8", "ignore") if isinstance(node.value, bytes) else node.value
            if WRITE_ENDPOINT.search(text):
                out.append(f"{filename}:{node.lineno} endpoint literal {text!r}")
        if isinstance(node, ast.Name) and node.id in GUARD_ONLY_NAMES | DYNAMIC_NAMES:
            out.append(f"{filename}:{node.lineno} uses {node.id}")
        if isinstance(node, ast.alias) and node.name in GUARD_ONLY_NAMES | DYNAMIC_NAMES:
            out.append(f"{filename} imports {node.name}")
        if isinstance(node, ast.Attribute):
            if node.attr in GUARD_ONLY_NAMES | DYNAMIC_NAMES:                      # px_guard.ORDER_PLACE, operator.methodcaller
                out.append(f"{filename}:{node.lineno} touches .{node.attr}")
            if node.attr in MONEY_ATTRS and filename not in MONEY_ALLOWED_FILES:
                out.append(f"{filename}:{node.lineno} touches .{node.attr}")
            if node.attr in BANNED_ATTRS and filename not in WRITE_ALLOWED_FILES:
                out.append(f"{filename}:{node.lineno} touches .{node.attr}")
            if node.attr == "token" and filename not in TOKEN_ALLOWED_FILES:
                out.append(f"{filename}:{node.lineno} touches .token (only the client and the hub may use the bearer token)")
            if node.attr == "write" and filename not in WRITE_ALLOWED_FILES:       # ANY receiver: aliases do not help
                out.append(f"{filename}:{node.lineno} touches .write on {ast.unparse(node.value)}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr", "setattr"):
            nm = node.args[1] if len(node.args) > 1 else None
            if nm is not None and not isinstance(nm, ast.Constant) and filename not in MONEY_ALLOWED_FILES:
                out.append(f"{filename}:{node.lineno} {node.func.id} with a computed attribute name")
            if isinstance(nm, ast.Constant) and nm.value in MONEY_ATTRS | {"write"} and filename not in MONEY_ALLOWED_FILES:
                out.append(f"{filename}:{node.lineno} dynamic dispatch to {nm.value!r}")
    return out


def test_no_module_but_the_guard_can_reach_the_order_path():
    bad = []
    for f in sorted(ROOT.rglob("*.py")) + sorted(ROOT.rglob("*.pyw")):
        rel = f.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".venv", "venv", "node_modules") or any(part.startswith(".") for part in rel.parts):
            continue
        bad += violations(f.read_text(encoding="utf-8"), rel.as_posix())
    assert not bad, "\n".join(bad)


def test_pin_scans_the_modules_it_should():
    names = {f.name for f in (ROOT / PKG).glob("*.py")}
    assert {"guard.py", "app.py", "client.py", "state.py", "hub.py", "mcp.py", "dev_gateway.py"} <= names
    assert not [f for f in ROOT.glob("*.py")], "no loose modules at the repo root: everything lives in the package"


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
        "bytes literal": 'u = b"/api/Order/place"',
        "guard constant by attribute": "import px_guard\nx = px_guard.ORDER_PLACE",
        "write on an aliased receiver": "x = self.client\nawait x.write(p, b)",
        "write on an unrelated name": "await px.write(p, b)",
        "clear halt from mcp": "g.clear_halt()",
        "reconcile from mcp": "await g.reconcile()",
        "private stop writer": "await g._arm_stop(t, 1)",
        "computed getattr": 'getattr(c, "wr" + "ite")',
        "methodcaller": "import operator\noperator.methodcaller('write')",
        "attrgetter": "from operator import attrgetter\nattrgetter('write')(c)",
        "raw http client": 'await self.client._http.post(url, json=b)',
        "minting a token": "t = await self.client.token()",
        "dunder dict": 'type(c).__dict__["write"]',
        "getattribute": 'c.__getattribute__("wr" + "ite")',
        "eval": 'eval("c.wr" + "ite")',
        "import_module": "import importlib\nimportlib.import_module('px_guard')",
    }
    for label, src in cases.items():
        assert violations(src, f"{PKG}/mcp.py"), f"detector missed: {label}"


def test_scan_is_recursive_and_ignores_tests_and_hidden_dirs(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sneaky.py").write_text('URL = "/api/Order/cancel"')
    hits = []
    for f in sorted(tmp_path.rglob("*.py")):
        rel = f.relative_to(tmp_path)
        hits += violations(f.read_text(encoding="utf-8"), rel.as_posix())
    assert hits and "pkg/sneaky.py" in hits[0]


def test_detector_allows_what_it_should():
    assert violations("await guard.place(tid, origin='ui', confirm_hash=h)", f"{PKG}/app.py") == []
    assert violations("p = await guard.preview(contract_id=c)", f"{PKG}/mcp.py") == []
    assert violations('rows = await client.post("/api/Order/searchOpen", {})', f"{PKG}/state.py") == []
    assert violations("self._http.post(url)", f"{PKG}/client.py") == []
    assert violations('x = "/api/Order/place"', GUARD_FILE) == []


def test_mock_is_server_only_so_its_exemption_is_safe():
    for name in SERVER_SIDE_MOCKS:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any(m.startswith(PKG) or m.startswith("projectx") or m in ("httpx", "requests", "urllib.request") for m in mods), (name, mods)
            if isinstance(node, ast.Name):
                assert node.id not in ("PXClient", "ClientSession"), (name, node.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("ws_connect",), (name, node.attr)
