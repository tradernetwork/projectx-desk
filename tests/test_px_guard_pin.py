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
MONEY_ATTRS = {"place", "flatten", "cancel_order", "cancel_entry", "close_position", "move_stop", "arm_trail",
               # the guard's other writers and human-only controls: only the web layer (a human's click) may reach them
               "clear_halt", "resolve_intent", "reconcile", "handle_position",
               "_arm_stop", "_naked", "_resize_stop", "_verify_stop_after_unknown"}
# Files are identified by their path relative to the repo (posix), never by bare filename: a stray app.py or client.py
# anywhere else in the tree must not inherit an exemption.
PKG = "projectx_desk"
GUARD_FILE = f"{PKG}/guard.py"
RH_GUARD_FILE = f"{PKG}/rh/guard.py"          # the second allowed writer (Robinhood, phase 1).
GUARD_FILES = {GUARD_FILE, RH_GUARD_FILE}
# Every Robinhood write tool name: a string constant with one of these (or any place_*/cancel_* *order* shape) outside a
# guard module fails. Read tools (get_*) are never on the list. Watchlist, alert and scanner writes are listed by their
# names. Checked against the live tool list captured 2026-11-17 (tests/fixtures/rh/tools_live_2026-11-17.json): every non-get tool in it
# must be on this list (test_every_live_rh_write_tool_is_pinned), so a new Robinhood tool forces a decision here.
RH_WRITE_TOOLS = {"place_equity_order", "cancel_equity_order", "review_equity_order", "place_option_order", "cancel_option_order", "replace_equity_order", "replace_option_order", "exercise_option", "cancel_option_exercise", "decline_trade_approval", "approve_trade_approval", "place_crypto_order", "cancel_crypto_order", "preview_crypto_order", "place_advanced_order", "cancel_advanced_order", "review_advanced_order", "review_option_order", "create_watchlist", "update_watchlist", "delete_watchlist", "add_to_watchlist", "remove_from_watchlist", "add_option_to_watchlist", "remove_option_from_watchlist", "follow_watchlist", "unfollow_watchlist", "create_alert", "update_alert", "delete_alert", "mark_alerts_read", "create_scan", "update_scan_config", "update_scan_filters", "run_scan", "preview_scan", "add_legend_indicator", "update_legend_indicator", "remove_legend_indicator", "set_legend_chart_settings", "save_scanner", "delete_scanner"}
RH_WRITE_SHAPE = re.compile(r"\b(place|cancel|replace|modify)_[a-z_]*order\b", re.I)
# Round 36: the Public tab is a READ-ONLY link to a separate sidecar process. Nothing in this tree may import its code or its broker
# SDK, name its writer machinery or a Public write method, or carry one of the sidecar's write routes as a literal. Checked as names,
# imports, attributes and string constants (docstrings included) everywhere the order-path pin looks, outside the two guards.
PUB_BANNED = {"public_desk", "public_api_sdk", "PublicApiClient", "harden_no_retry", "claim_writer",
              "place_order", "cancel_order", "place_multileg", "cancel_and_replace"}
PUB_BANNED_RE = re.compile(r"\b(" + "|".join(sorted(PUB_BANNED)) + r")\b")
PUB_WRITE_PATHS = ("/api/desk/place", "/api/desk/cancel", "/api/desk/halt", "/api/wheel/confirm", "/api/wheel/stage",
                   "/api/wheel/record", "/api/wheel/sync")
PUB_WRITE_RE = re.compile("|".join(re.escape(x) for x in PUB_WRITE_PATHS), re.I)
# Review of Round 36: the futures desk has no /api/desk/ or /api/wheel/ route of its own, so ANY string (or string built from
# constants, or the constant parts of an f-string) holding one of those prefixes is the sidecar's write surface.
PUB_ROUTE_PREFIX = re.compile(r"/api/(desk|wheel)/", re.I)
PUB_MODULE_RE = re.compile(r"public_?desk|public_?api", re.I)       # module names, any spelling of the two packages
# Files that may send an outbound non-GET HTTP request today, each for its own documented reason (the two guards and the mocks are
# exempt from every rule). Anywhere else, a write verb on the wire is a violation (call-shape rule below).
NON_GET_SENDERS = {f"{PKG}/client.py", f"{PKG}/push.py", f"{PKG}/chat.py", f"{PKG}/llm_login.py", f"{PKG}/llm_chat.py",
                   f"{PKG}/rh/oauth.py", f"{PKG}/rh/mcp.py"}
SUBPROCESS_ALLOWED = {f"{PKG}/llm_providers.py", "scripts/deskagent.py", "scripts/screenshots.py"}
WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE"}
# ProjectX reads are POSTs to a constant /api/<Capitalised>/ path through PXClient (the order-write paths are banned above); those
# stay allowed. A post to anything else, or to a URL computed at run time, is a send.
PROJECTX_PATH = re.compile(r"/api/[A-Z][A-Za-z]+/[A-Za-z]+$")
# KNOWN LIMITS of this static pin (cannot be caught by AST folding; covered by review, the GET-only rule on pub/, and the
# fact that the sidecar's POSTs need its own CSRF token, which nothing here fetches):
#   * a route or name assembled from runtime data (a variable, a file, an env var, a network reply), e.g. f"/api/x/{v}" with
#     v not a constant, or "".join(reversed(s)) of a variable;
#   * a write sent through a module this pin does not model (raw socket, http.client, asyncio.open_connection, ctypes), or a
#     function object fetched through a container or a closure;
#   * a non-GET sent by one of NON_GET_SENDERS to a URL built at run time (those files are reviewed by hand; none names the
#     sidecar today, and the folded-string rule still catches a constant one).


def _fold(node):
    """The string a node evaluates to when it is built only from constants, else None. f-string holes that are not constant
    fold to a NUL placeholder, so a constant prefix like "/api/desk/" is still seen."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bytes):
            return v.decode("utf-8", "ignore")
        return v if isinstance(v, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _fold(node.left), _fold(node.right)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                f = _fold(part.value)
                out.append(f if f is not None else "\x00")
            else:
                out.append(_fold(part) or "")
        return "".join(out)
    if isinstance(node, ast.Call):
        f, args = node.func, node.args
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if name == "join" and isinstance(f, ast.Attribute) and len(args) == 1:
            sep = _fold(f.value)
            items = args[0]
            if isinstance(items, ast.Call) and isinstance(items.func, ast.Name) and items.func.id == "map" and len(items.args) == 2 \
                    and isinstance(items.args[0], ast.Name) and items.args[0].id == "chr" and isinstance(items.args[1], (ast.List, ast.Tuple)):
                vals = [e.value for e in items.args[1].elts if isinstance(e, ast.Constant) and isinstance(e.value, int)]
                if sep is not None and len(vals) == len(items.args[1].elts):
                    try:
                        return sep.join(map(chr, vals))
                    except (ValueError, OverflowError):
                        return None
            if sep is not None and isinstance(items, (ast.List, ast.Tuple)):
                parts = [_fold(e) for e in items.elts]
                return sep.join(parts) if all(x is not None for x in parts) else None
        if name == "chr" and len(args) == 1 and isinstance(args[0], ast.Constant) and isinstance(args[0].value, int):
            try:
                return chr(args[0].value)
            except (ValueError, OverflowError):
                return None
        if name in ("decode", "encode", "str", "lower", "upper", "strip") and isinstance(f, ast.Attribute) and not args:
            return _fold(f.value)
        if name in ("b64decode", "urlsafe_b64decode", "b32decode", "b16decode") and args:
            raw = _fold(args[0])
            if raw is not None:
                import base64
                try:
                    return getattr(base64, name)(raw).decode("utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    return None
        if name == "decode" and len(args) >= 2:
            raw, codec = _fold(args[0]), _fold(args[1])
            if raw is not None and codec and codec.replace("-", "_").lower() in ("rot13", "rot_13"):
                import codecs
                return codecs.decode(raw, "rot13")
    return None


def _built_text_violations(node, filename):
    """The text rules, applied to a string BUILT from constants (concatenation, f-string, join, chr, base64, rot13)."""
    text = _fold(node)
    if text is None:
        return []
    out = []
    if WRITE_ENDPOINT.search(text):
        out.append(f"{filename}:{node.lineno} built endpoint literal {text[:40]!r}")
    if RH_WRITE_SHAPE.search(text) or any(re.search(rf"\b{t}\b", text) for t in RH_WRITE_TOOLS):
        out.append(f"{filename}:{node.lineno} built Robinhood write tool name {text[:40]!r}")
    if PUB_BANNED_RE.search(text) or PUB_MODULE_RE.search(text):
        out.append(f"{filename}:{node.lineno} built Public sidecar or SDK name {text[:40]!r}")
    if PUB_WRITE_RE.search(text) or PUB_ROUTE_PREFIX.search(text):
        out.append(f"{filename}:{node.lineno} built Public sidecar route {text[:40]!r}")
    return out


def _verb(node):
    """The HTTP method a call names: folded upper-case text, or '?' when it is computed at run time."""
    t = _fold(node)
    return t.strip().upper() if t is not None else "?"


def _call_shape_violations(tree, filename):
    """A write verb on the wire outside NON_GET_SENDERS: httpx/requests-style .post/.put/.patch/.delete, .request or .stream or
    .build_request with a method that is not a constant GET/HEAD, urllib Request with data or a write method, urlopen with
    data, and subprocess (curl, wget) outside SUBPROCESS_ALLOWED. FastAPI's @app.post decorators are route definitions, not
    sends, and are skipped."""
    out = []
    decorators = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decorators |= {id(d) for d in n.decorator_list}
    sender = filename in NON_GET_SENDERS
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) and n.module else [])
            if any(m.split(".")[0] == "subprocess" for m in mods) and filename not in SUBPROCESS_ALLOWED:
                out.append(f"{filename}:{n.lineno} imports subprocess")
            if any(PUB_MODULE_RE.search(m) for m in mods):
                out.append(f"{filename}:{n.lineno} imports {mods} (Public is read-only here)")
        if not isinstance(n, ast.Call) or id(n) in decorators or sender:
            continue
        f = n.func
        attr = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        first = _fold(n.args[0]) if n.args else None
        projectx_read = attr == "post" and first is not None and PROJECTX_PATH.match(first) and not WRITE_ENDPOINT.search(first)
        if attr in ("post", "put", "patch", "delete") and isinstance(f, ast.Attribute) and not projectx_read:
            out.append(f"{filename}:{n.lineno} sends .{attr}() (non-GET HTTP outside the allowed senders)")
        if attr in ("request", "stream", "build_request") and isinstance(f, ast.Attribute):
            m = n.args[0] if n.args else kw.get("method")
            if m is None or _verb(m) not in ("GET", "HEAD"):
                out.append(f"{filename}:{n.lineno} .{attr}() with method {_verb(m) if m is not None else '?'}")
        if attr == "Request":
            m = kw.get("method")
            if "data" in kw or len(n.args) > 1 or (m is not None and _verb(m) not in ("GET", "HEAD")):
                out.append(f"{filename}:{n.lineno} urllib Request with a body or a write method")
        if attr == "urlopen" and ("data" in kw or len(n.args) > 1):
            out.append(f"{filename}:{n.lineno} urlopen with data")
        if attr in ("system", "popen", "spawnl", "spawnv", "execv", "execl") and isinstance(f, ast.Attribute) \
                and isinstance(f.value, ast.Name) and f.value.id == "os":
            out.append(f"{filename}:{n.lineno} os.{attr}")
    return out
MONEY_ALLOWED_FILES = {f"{PKG}/app.py"}       # guard.py itself is exempt from every rule
EXTRA_ALLOWED = {"arm_trail": {f"{PKG}/mcp.py"}}   # the MCP tool is the one other caller of arm_trail, gated by PX_MCP_TRAIL and the human-typed window


def money_ok(name, filename):
    return filename in MONEY_ALLOWED_FILES or filename in EXTRA_ALLOWED.get(name, set())
WRITE_ALLOWED_FILES = {f"{PKG}/client.py"}    # defines PXClient.write; may not CALL it
DYNAMIC_NAMES = {"methodcaller", "attrgetter", "__import__", "import_module", "eval", "exec", "vars"}   # build a name or call at runtime
BANNED_ATTRS = {"__dict__", "__getattribute__", "_http"}        # reach around the client wrapper or the attribute check
TOKEN_ALLOWED_FILES = {f"{PKG}/client.py", f"{PKG}/hub.py"}              # only these may hold or mint the bearer token
# Server-side mocks SERVE the endpoints and never call them. Exempt by name, and test_mock_is_server_only
# proves they import no client code, so the exemption cannot quietly become a second order path.
SERVER_SIDE_MOCKS = {f"{PKG}/dev_gateway.py", f"{PKG}/dev_robinhood.py"}


def violations(source: str, filename: str):
    if filename in GUARD_FILES or filename in SERVER_SIDE_MOCKS:
        return []
    out, tree = [], ast.parse(source)
    out += _call_shape_violations(tree, filename)
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
            out += _built_text_violations(node, filename)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            t = node.value.decode("utf-8", "ignore") if isinstance(node.value, bytes) else node.value
            if PUB_ROUTE_PREFIX.search(t):
                out.append(f"{filename}:{node.lineno} Public sidecar route prefix in {t[:40]!r}")
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value.decode("utf-8", "ignore") if isinstance(node.value, bytes) else node.value
            if WRITE_ENDPOINT.search(text):
                out.append(f"{filename}:{node.lineno} endpoint literal {text!r}")
            if RH_WRITE_SHAPE.search(text) or any(re.search(rf"\b{t}\b", text) for t in RH_WRITE_TOOLS):
                out.append(f"{filename}:{node.lineno} Robinhood write tool name in {text[:40]!r}")
            if PUB_BANNED_RE.search(text):
                out.append(f"{filename}:{node.lineno} Public sidecar or SDK name in {text[:40]!r}")
            if PUB_WRITE_RE.search(text):
                out.append(f"{filename}:{node.lineno} Public sidecar write route in {text[:40]!r}")
        if isinstance(node, ast.Name) and node.id in GUARD_ONLY_NAMES | DYNAMIC_NAMES:
            out.append(f"{filename}:{node.lineno} uses {node.id}")
        if isinstance(node, ast.Name) and node.id in PUB_BANNED:
            out.append(f"{filename}:{node.lineno} uses {node.id} (Public is read-only here)")
        if isinstance(node, ast.alias) and any(part in PUB_BANNED for part in node.name.split(".")):
            out.append(f"{filename} imports {node.name} (Public is read-only here)")
        if isinstance(node, ast.ImportFrom) and node.module and any(part in PUB_BANNED for part in node.module.split(".")):
            out.append(f"{filename} imports from {node.module} (Public is read-only here)")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in PUB_BANNED:
            out.append(f"{filename}:{node.lineno} defines {node.name} (Public is read-only here)")
        if isinstance(node, ast.alias) and node.name in GUARD_ONLY_NAMES | DYNAMIC_NAMES:
            out.append(f"{filename} imports {node.name}")
        if isinstance(node, ast.Attribute):
            if node.attr in GUARD_ONLY_NAMES | DYNAMIC_NAMES:                      # px_guard.ORDER_PLACE, operator.methodcaller
                out.append(f"{filename}:{node.lineno} touches .{node.attr}")
            if node.attr in PUB_BANNED:
                out.append(f"{filename}:{node.lineno} touches .{node.attr} (Public is read-only here)")
            if node.attr in MONEY_ATTRS and not money_ok(node.attr, filename):
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
            if isinstance(nm, ast.Constant) and nm.value in MONEY_ATTRS | {"write"} and not money_ok(nm.value, filename):
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
        # Round 36: the Public sidecar link
        "public desk import": "import public_desk",
        "public desk submodule import": "from public_desk.desk import guard",
        "public desk dotted import": "import public_desk.desk.guard as g",
        "public sdk import": "from public_api_sdk import PublicApiClient",
        "public sdk alias": "import public_api_sdk as sdk",
        "public client name": "c = PublicApiClient()",
        "retry switch": "client.harden_no_retry(c)",
        "writer claim": "claim_writer()",
        "public place": "await c.place_order(o)",
        "public cancel": "c.cancel_order(i)",
        "public multileg": "c.place_multileg(o)",
        "public replace": "c.cancel_and_replace(o)",
        "public name in a string": 'm = "public_desk.desk.guard"',
        "sidecar place route": 'u = "http://127.0.0.1:8700/api/desk/place"',
        "sidecar cancel route": 'u = base + "/api/desk/cancel"',
        "sidecar halt resolve": 'u = "/api/desk/halt/resolve"',
        "sidecar wheel confirm": 'u = "/api/wheel/confirm"',
        "sidecar wheel stage": 'u = "/API/WHEEL/STAGE"',
        "sidecar wheel record": 'u = "/api/wheel/record"',
        "sidecar wheel sync": 'u = "/api/wheel/sync"',
        "defining a writer": "def place_multileg(x):\n    pass",
        # review of Round 36: strings built from constants, and write verbs on the wire
        "getattr concat": 'getattr(x, "place_" + "order")(o)',
        "importlib concat": 'import importlib\nimportlib.import_module("public_" + "desk")',
        "__import__ concat": '__import__("public_" + "desk")',
        "implicit concat route": 'u = "/api/desk/pl" "ace"',
        "fstring route with a variable": 'v = "place"\nu = f"/api/desk/{v}"',
        "fstring const": "u = f\"/api/desk/{'pla'}ce\"",
        "concat route": 'u = "/api/desk/" + "place"',
        "join route": 'u = "/".join(["", "api", "desk", "place"])',
        "base64 route": 'import base64\nu = base64.b64decode("L2FwaS9kZXNrL3BsYWNl").decode()',
        "chr route": 'u = "/api/" + "".join(map(chr, [100, 101, 115, 107])) + "/x"',
        "chr name": 'n = "".join(map(chr, [112, 108, 97, 99, 101, 95, 111, 114, 100, 101, 114]))',
        "rot13 name": 'import codecs\ncodecs.decode("cynpr_beqre", "rot13")',
        "httpx post": 'import httpx\nhttpx.post("http://127.0.0.1:8701/x", json={})',
        "client post to a computed url": "await c.post(u)",
        "request POST": 'await c.request("POST", u)',
        "request computed method": "await c.request(m, u)",
        "stream POST": 'c.stream("POST", u)',
        "stream concat POST": 'c.stream("PO" + "ST", u)',
        "build_request PUT": 'c.build_request("PUT", u)',
        "client delete": "await c.delete(u)",
        "urllib Request with data": 'import urllib.request\nurllib.request.urlopen(urllib.request.Request(u, data=b"{}"))',
        "urllib Request write method": 'import urllib.request\nurllib.request.Request(u, method="PO" + "ST")',
        "urlopen with data": 'import urllib.request\nurllib.request.urlopen(u, b"{}")',
        "subprocess curl": 'import subprocess\nsubprocess.run(["curl", "-X", "POST", u])',
        "os.system": 'import os\nos.system("curl -X POST " + u)',
        "alt module spelling": "import publicdesk",
        "alt sdk spelling": "from publicapi import Client",
        "sys.modules lookup": 'import sys\nsys.modules["public_desk"]',
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


def test_public_rules_hold_in_every_file_but_the_guards():
    for f in ("pub/link.py", "pub/view.py", "digest.py", "app.py", "mcp.py", "rh/state.py"):
        assert violations("import public_desk", f"{PKG}/{f}"), f
        assert violations('u = "/api/desk/place"', f"{PKG}/{f}"), f


def test_the_public_link_reads_get_only():
    """pub/link.py and pub/view.py carry no HTTP method but GET: no POST/PUT/DELETE/PATCH literal, no attribute that sends a
    request another way, and every `.stream(...)` call names GET as a constant."""
    pub = ROOT / PKG / "pub"
    files = sorted(pub.glob("*.py"))
    assert {f.name for f in files} >= {"link.py", "view.py"}
    streams = 0
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                v = n.value.strip()
                # "options" (lower case) is the sidecar's options switch in the mode block; the HTTP verb is upper case
                assert v.upper() not in ("POST", "PUT", "DELETE", "PATCH", "CONNECT", "TRACE") and v != "OPTIONS", (f.name, v)
            if isinstance(n, ast.Attribute):
                assert n.attr not in ("post", "put", "delete", "patch", "request", "send", "build_request", "options"), (f.name, n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "stream":
                streams += 1
                assert n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "GET", f.name
    assert streams == 1


def test_no_static_file_names_a_sidecar_write_route_or_port():
    for f in sorted((ROOT / PKG / "static").glob("*")):
        if f.suffix not in (".js", ".html", ".css"):
            continue
        text = f.read_text(encoding="utf-8")
        assert not PUB_WRITE_RE.search(text), f.name
        assert not PUB_BANNED_RE.search(text), f.name
        assert ":8700" not in text, f.name


def test_call_shapes_allow_what_the_desk_does_today():
    ok = [
        '@app.post("/api/place")\nasync def f():\n    pass',                       # a FastAPI route, not a send
        'async with c.stream("GET", url) as r:\n    pass',
        'rows = await client.post("/api/Order/searchOpen", {})',                    # a ProjectX read through PXClient
        'r = urllib.request.urlopen(BASE + path, timeout=10)',
    ]
    for src in ok:
        assert violations(src, f"{PKG}/state.py") == [], src
    assert violations('async with c.stream("POST", url) as r:\n    pass', f"{PKG}/llm_chat.py") == []   # an allowed sender
    assert violations("import subprocess", f"{PKG}/llm_providers.py") == []
    assert violations("import subprocess", f"{PKG}/digest.py")


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


def test_every_live_rh_write_tool_is_pinned():
    """The tool list captured from the real Robinhood MCP (names only, no account data): anything that is not a get_* read must be pinned."""
    import json
    d = json.loads((ROOT / "tests" / "fixtures" / "rh" / "tools_live_2026-11-17.json").read_text(encoding="utf-8"))
    names = {t["name"] for t in (d["tools"] if isinstance(d, dict) else d)}
    unpinned = sorted(n for n in names if not n.startswith("get_") and n != "search" and n not in RH_WRITE_TOOLS)
    assert unpinned == [], f"live Robinhood tools that mutate or review but are not pinned: {unpinned}"
    assert len(names) == 96
