"""Round 36: the Public tab in the app and the digest. Route presence, record-only absence, stream safety, /state.json,
boot log redaction, the digest PUB lines, the deadline loop under a hung sidecar, and isolation from the Topstep numbers."""
import ast
import asyncio
import json
import pathlib
import time
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from projectx_desk import app as app_mod
from projectx_desk import digest as px_digest
from projectx_desk.app import create_app, _deadline_loop, _deadline_age
from projectx_desk.hub import SignalRHub
from projectx_desk.pub import link as pub_link, view as pub_view
from tests.test_pub_link import FIX, hung_server  # noqa: F401 - the fixture is used by name
from tests.test_px_app import FakeClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "projectx_desk"


def mock_link(payload=None, seen=None):
    data = payload if payload is not None else FIX.read_bytes()

    def h(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(200, content=data)
    return pub_link.PubLink("http://127.0.0.1:8700", transport=httpx.MockTransport(h))


def down_link():
    def h(req):
        raise httpx.ConnectError("refused")
    return pub_link.PubLink("http://127.0.0.1:8700", transport=httpx.MockTransport(h))


def make(record_only=False, **kw):
    fake = FakeClient()
    hub = SignalRHub(fake, "market", url="ws://127.0.0.1:1/x")
    uhub = None if record_only else SignalRHub(fake, "user", url="ws://127.0.0.1:1/y")
    return create_app(client=fake, market_hub=hub, user_hub=uhub, allowed_hosts={"testserver"}, connect=False,
                      record_only=record_only, **kw)


def paths(app):
    return {getattr(r, "path", "") for r in app.routes}


# ---- F17: route only when configured -------------------------------------------------------------------------------
def test_no_url_means_no_route_and_no_link(monkeypatch):
    monkeypatch.delenv("PX_PUB_URL", raising=False)
    app = make()
    assert "/api/pub/state" not in paths(app) and app.state.pub is None
    with TestClient(app) as c:
        assert c.get("/api/pub/state").status_code == 404
        assert "pub" not in c.get("/state.json").json()
        assert c.get("/ai/state").json()["pub"] is None
        assert "PUB" not in c.get("/ai/digest").text


def test_url_from_env_registers_the_get_route_only(monkeypatch):
    monkeypatch.setenv("PX_PUB_URL", "http://127.0.0.1:8700")
    app = make()
    assert "/api/pub/state" in paths(app) and app.state.pub.base == "http://127.0.0.1:8700"
    methods = {m for r in app.routes if getattr(r, "path", "") == "/api/pub/state" for m in r.methods}
    assert methods <= {"GET", "HEAD"}
    assert not [p for p in paths(app) if p.startswith("/api/pub") and p != "/api/pub/state"]


def test_route_serves_the_reduced_view():
    app = make(pub=mock_link())
    with TestClient(app) as c:
        v = c.get("/api/pub/state").json()
    assert v["reachable"] is True and v["positions"][0]["symbol"] == "ACME" and v["balances"]["equity"] == "12345.67"
    assert "tkt-invented-0001" not in json.dumps(v)


# ---- F18: record-only ----------------------------------------------------------------------------------------------
def test_record_only_has_no_route_no_link_and_never_fetches(monkeypatch):
    monkeypatch.setenv("PX_PUB_URL", "http://127.0.0.1:8700")
    seen = []
    app = make(record_only=True, pub=mock_link(seen=seen))
    assert "/api/pub/state" not in paths(app) and app.state.pub is None
    with TestClient(app) as c:
        assert c.get("/api/pub/state").status_code == 404
    assert seen == []


# ---- F19: stream ---------------------------------------------------------------------------------------------------
def test_stream_route_drops_every_dollar_field():
    seen = []
    app = make(pub=mock_link(seen=seen))
    with TestClient(app) as c:
        v = c.get("/api/pub/state?stream=1").json()
    text = json.dumps(v)
    for k in pub_view.DOLLARS:
        assert f'"{k}"' not in text
    for n in ("12345.67", "4400.00", "41.35", "18.25", "4210.00"):
        assert n not in text
    assert "balances" not in v and seen[0].url.query == b"stream=1"


# ---- F20: /state.json ----------------------------------------------------------------------------------------------
def test_state_json_reports_reachability_without_fetching():
    seen = []
    app = make(pub=mock_link(seen=seen))
    with TestClient(app) as c:
        assert c.get("/state.json").json()["pub"] == {"enabled": True, "reachable": None, "code": None, "age_s": None}
        assert seen == []
        c.get("/api/pub/state")
        p = c.get("/state.json").json()["pub"]
        assert p["reachable"] is True and p["code"] is None and isinstance(p["age_s"], int) and len(seen) == 1


def test_state_json_shows_the_down_code():
    app = make(pub=down_link())
    with TestClient(app) as c:
        c.get("/api/pub/state")
        assert c.get("/state.json").json()["pub"] == {"enabled": True, "reachable": False, "code": "unreachable", "age_s": None}


# ---- F21: the URL is never printed ---------------------------------------------------------------------------------
def test_boot_log_names_no_url(monkeypatch, capsys):
    monkeypatch.setenv("PX_PUB_URL", "http://127.0.0.1:8700")
    make()
    out = capsys.readouterr().out
    assert "Public link: on" in out and "8700" not in out
    monkeypatch.setenv("PX_PUB_URL", "http://admin:hunter2@10.0.0.9:8700")
    make()
    out = capsys.readouterr().out
    assert "Public link off: not_allowed" in out and "hunter2" not in out and "10.0.0.9" not in out and "admin" not in out


# ---- F22: a hung sidecar never stalls the deadline loop -------------------------------------------------------------
def test_deadline_loop_keeps_ticking_while_the_sidecar_hangs(hung_server):  # noqa: F811
    app = types.SimpleNamespace(state=types.SimpleNamespace(guard=None))
    lk = pub_link.PubLink(f"http://127.0.0.1:{hung_server}")

    async def run():
        dl = asyncio.create_task(_deadline_loop(app))
        fetch = asyncio.create_task(lk.state())
        ages = []
        t0 = time.monotonic()
        while not fetch.done():
            await asyncio.sleep(0.2)
            ages.append(_deadline_age(app))
        took = time.monotonic() - t0
        dl.cancel()
        return fetch.result(), ages, took
    v, ages, took = asyncio.run(run())
    assert v["code"] == "timeout" and took < 3.8
    assert len(ages) >= 10 and all(a is not None and a < 1.6 for a in ages[2:])


def test_a_hung_sidecar_route_answers_by_the_timeout(hung_server):  # noqa: F811
    app = make(pub=pub_link.PubLink(f"http://127.0.0.1:{hung_server}"))
    with TestClient(app) as c:
        t0 = time.monotonic()
        v = c.get("/api/pub/state").json()
        assert time.monotonic() - t0 < 3.8 and v["code"] == "timeout"
        txt = c.get("/ai/digest").text                         # served from the cached failure: no second wait
    assert "PUB: UNKNOWN code=timeout" in txt


# ---- F23 to F26: the digest ----------------------------------------------------------------------------------------
def test_ai_state_carries_the_block_and_the_digest_prints_pub_lines():
    app = make(pub=mock_link())
    with TestClient(app) as c:
        st = c.get("/ai/state").json()
        txt = c.get("/ai/digest").text
    assert st["pub"]["reachable"] is True
    lines = [ln for ln in txt.splitlines() if ln.startswith("PUB")]
    heads = [ln.split(":", 1)[0] for ln in lines]
    assert heads == ["PUB", "PUB_POS", "PUB_ORD", "PUB_TKT", "PUB_WHEEL", "PUB_WASH"]
    assert lines[0].startswith("PUB: OK ") and "trading=NO" in lines[0] and "equity=12345.67" in lines[0]
    assert "PUB_POS: n=2 ACME EQUITY qty=100 cost=4210.00" in txt
    assert "ACME261218P00040000 OPTION qty=-1 cost=UNKNOWN value=-35.00 upl=UNKNOWN" in txt
    assert "PUB_TKT: n=1 placed ZETA BUY EQUITY qty=5 limit=18.25" in txt
    assert "ACME basis=KNOWN:41.35 shares=100" in txt and "BOLT basis=UNKNOWN shares=0" in txt
    assert "PUB_WASH: n=1 BOLT=POSSIBLE(2026-10-20..2026-12-19) mode=warn" in txt
    assert all(len(ln) <= 400 for ln in lines)
    assert "tkt-invented" not in txt and "ev-invented" not in txt


def test_halt_banner_line_carries_the_cause_word_only():
    d = json.loads(FIX.read_text())
    d["halt"] = {"halted": True, "cause": "place_unknown"}
    lines = px_digest.pub_lines(pub_view.reduce(json.loads(json.dumps(d))))
    assert lines[1].startswith("PUB_HALT: cause=place_unknown")
    assert "halted=YES" in lines[0]


def test_a_down_sidecar_prints_unknown_never_ok_or_empty():
    app = make(pub=down_link())
    with TestClient(app) as c:
        txt = c.get("/ai/digest").text
    lines = [ln for ln in txt.splitlines() if ln.startswith("PUB")]
    assert lines == ["PUB: UNKNOWN code=unreachable"]
    assert px_digest.pub_lines(None) == [] and px_digest.pub_lines({"enabled": True, "reachable": False}) == ["PUB: UNKNOWN code=unreachable"]


def test_lists_are_capped_at_8_items_and_400_chars():
    d = json.loads(FIX.read_text())
    d["positions"] = [{"symbol": f"S{i}", "asset": "EQUITY", "qty": str(i), "market_value": "123456789.12",
                       "cost_basis": "123456789.12", "unrealized_pl": "-123456789.12", "unknown": []} for i in range(30)]
    lines = px_digest.pub_lines(pub_view.reduce(d))
    pos = next(ln for ln in lines if ln.startswith("PUB_POS"))
    assert len(pos) <= 400 and "S8 " not in pos and "S9 " not in pos
    d["positions"] = d["positions"][:12]
    for p in d["positions"]:
        p.update(market_value="1", cost_basis="1", unrealized_pl="1")
    pos = next(ln for ln in px_digest.pub_lines(pub_view.reduce(d)) if ln.startswith("PUB_POS"))
    assert pos.startswith("PUB_POS: n=12 ") and "(+4 more)" in pos


def test_stream_digest_has_no_dollar_in_pub_lines():
    app = make(pub=mock_link())
    with TestClient(app) as c:
        txt = c.get("/ai/digest?stream=1").text
    lines = [ln for ln in txt.splitlines() if ln.startswith("PUB")]
    assert lines and lines[0].startswith("PUB: OK")
    joined = "\n".join(lines)
    for bad in ("equity=", "cash=", "bp=", "cost=", "value=", "upl=", "px=", "avg=", "limit=", ":41.35", "12345.67", "18.25", "4400"):
        assert bad not in joined, bad


def test_the_notifier_state_is_built_without_the_public_block():
    src = (PKG / "app.py").read_text(encoding="utf-8")
    assert "px_digest.build_ai_state(app, c, with_pub=False)" in src
    seen = []
    app = make(pub=mock_link(seen=seen))
    with TestClient(app):
        st = asyncio.run(px_digest.build_ai_state(app, None, with_pub=False))
    assert st["pub"] is None and seen == []


# ---- F27: isolation from the Topstep numbers ------------------------------------------------------------------------
def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in n.names}
    return out


@pytest.mark.parametrize("name", ["guard.py", "sizing.py", "limits.py", "anchor.py"])
def test_order_path_and_sizing_never_import_the_public_link(name):
    imps = _imports(PKG / name)
    assert not [m for m in imps if "pub" in m.split(".") or m.startswith("projectx_desk.pub")], imps


def test_the_pub_package_imports_nothing_of_the_desk_but_itself():
    for f in (PKG / "pub").glob("*.py"):
        for m in _imports(f):
            if m.startswith("projectx_desk"):
                assert m.startswith("projectx_desk.pub"), (f.name, m)
            assert not m.startswith(("public_desk", "public_api_sdk")), (f.name, m)


def test_the_topstep_risk_block_is_identical_with_and_without_public():
    def risk(pub):
        app = make(pub=pub)
        with TestClient(app) as c:
            s = c.get("/ai/state").json()
        return s["risk"], s["size"], s["kpi"], s["positions"], s["orders"]
    assert risk(None) == risk(mock_link())


def test_the_pub_package_never_opens_a_file():
    for f in (PKG / "pub").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                assert n.func.id not in ("open", "print"), f.name
            if isinstance(n, ast.Attribute):
                assert n.attr not in ("write_text", "write_bytes", "open", "mkdir", "unlink", "replace"), (f.name, n.attr)


# ---- F33: nothing on disk, so the boot check and the agent's deny list need nothing new -----------------------------------
def test_boot_check_and_agent_deny_list_are_unaffected(tmp_path, monkeypatch):
    assert "PX_PUB" not in (PKG / "datacheck.py").read_text(encoding="utf-8")
    from projectx_desk import datacheck
    env = {"PX_PUB_URL": "http://127.0.0.1:8700"}
    assert datacheck.problem(env, ROOT) == datacheck.problem({}, ROOT)
    made = []
    for i, pub in enumerate((None, mock_link())):            # the same files appear with and without the link
        d = tmp_path / str(i)
        d.mkdir()
        monkeypatch.chdir(d)
        with TestClient(make(pub=pub)) as c:
            c.get("/api/pub/state")
            c.get("/ai/digest")
        made.append(sorted(str(x.relative_to(d)) for x in d.rglob("*")))
    assert made[0] == made[1]
    src = (ROOT / "scripts" / "deskagent.py").read_text(encoding="utf-8")
    assert "pub" not in src.split("DENIED = [", 1)[1].split("]", 1)[0].lower()
