"""Thesis card: allowlisted rows, append-only store, fact checks, the journal join, the routes' gate, and the pins."""
import ast
import json
import pathlib
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from projectx_desk import journal as j
from projectx_desk import thesis as th
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub
from test_journal import raw, MES
from test_px_app import FakeClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "projectx_desk" / "static"


def card(**o):
    d = {"contract": MES, "type": "SCALP", "target": 7710.0, "invalidation": 7695.0, "limit_s": 300}
    d.update(o)
    return d


# ---- clean ---------------------------------------------------------------------------------------------------------------

def test_clean_keeps_only_the_allowlist_and_implies_the_side():
    r = th.clean({**card(), "account": 28659217, "note": "buy now", "ts": 1}, now_ms=5)
    assert set(r) == {"ts", "contract", "type", "side", "target", "invalidation", "limit_s"}
    assert r["side"] == "LONG" and r["ts"] == 5 and "28659217" not in json.dumps(r)
    assert th.clean(card(target=7690.0, invalidation=7700.0))["side"] == "SHORT"


@pytest.mark.parametrize("bad", [
    None, [], {}, card(type="YOLO"), card(contract="evil; DROP"), card(target=float("nan")), card(target="7710"), card(target=True),
    card(target=-1), card(invalidation=0), card(limit_s=0), card(limit_s=10 ** 7), card(limit_s="5"), card(side="UP"),
    card(target=7700.0, invalidation=7700.0), card(side="SHORT"),
])
def test_bad_cards_are_refused(bad):
    assert th.clean(bad) is None


def test_target_and_invalidation_are_optional():
    r = th.clean(card(target=None, invalidation=None, limit_s=None))
    assert r and r["side"] is None and th.check(r, 1.0, 1.0, r["ts"] + 10 ** 9)["invalidation_broken"] is None


# ---- store ---------------------------------------------------------------------------------------------------------------

def test_store_is_append_only_and_skips_bad_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    s = th.Store(p)
    a = s.add(th.clean(card(), now_ms=100))
    s.add(th.clean(card(type="FADE_RANGE"), now_ms=200))
    first = p.read_text().splitlines()[0]
    p.write_text(p.read_text() + "garbage\n" + json.dumps({"ts": 1, "type": "SCALP"}) + "\n")
    s2 = th.Store(p)
    assert [r["ts"] for r in s2.rows] == [100, 200] and s2.latest(MES)["type"] == "FADE_RANGE" and s2.latest("CON.F.US.MNQ.Z26") is None
    assert p.read_text().splitlines()[0] == first and a["ts"] == 100


# ---- checks (facts only) -------------------------------------------------------------------------------------------------

def test_checks_report_invalidation_overrun_and_wide_stop():
    t = th.clean(card(), now_ms=0)                        # LONG, inval 7695, scalp limit 300 s
    c = th.check(t, last=7700.0, stop=7696.0, now_ms=100_000)
    assert c == {"invalidation_broken": False, "scalp_overran": False, "stop_wider_than_invalidation": False, "elapsed_s": 100, "limit_s": 300}
    assert th.facts(t, c) == []
    c = th.check(t, last=7695.0, stop=7690.0, now_ms=301_000)
    assert c["invalidation_broken"] and c["scalp_overran"] and c["stop_wider_than_invalidation"]
    assert len(th.facts(t, c)) == 3
    s = th.clean(card(target=7690.0, invalidation=7700.0), now_ms=0)
    assert th.check(s, last=7700.0, stop=7701.0, now_ms=1)["invalidation_broken"] and th.check(s, 7699.0, 7701.0, 1)["stop_wider_than_invalidation"]


def test_only_a_scalp_has_an_overrun_flag():
    t = th.clean(card(type="TREND_ADD"), now_ms=0)
    assert th.check(t, 7700.0, None, 10 ** 9)["scalp_overran"] is None


# ---- journal join --------------------------------------------------------------------------------------------------------

def ms(h, m):
    return int(datetime(2026, 10, 29, h, m, tzinfo=timezone.utc).timestamp() * 1000)


def test_trip_joins_its_thesis_once_and_the_memory_prints_a_fact_line():
    trips = j.round_trips(j.fill_rows([raw(10, 0, 7700, ts="2026-10-29T14:10:00+00:00"),
                                       raw(11, 1, 7712, pnl=60.0, ts="2026-10-29T14:20:00+00:00"),
                                       raw(12, 0, 7710, ts="2026-10-29T14:40:00+00:00"),
                                       raw(13, 1, 7694, pnl=-80.0, ts="2026-10-29T14:50:00+00:00")]))
    rows = [th.clean(card(), now_ms=ms(14, 8)), th.clean(card(type="REVERSAL_HOLD", target=7720.0), now_ms=ms(14, 5)),
            th.clean(card(type="FADE_RANGE"), now_ms=ms(23, 0))]
    j.attach_context(trips, {})
    th.attach(trips, rows)
    assert trips[0]["thesis"]["ts"] == ms(14, 8)                       # the newest card inside the window
    assert trips[1]["thesis"] is None                                  # the 14:05 card is too early for it; the 23:00 one is after
    line = th.trip_line(trips[0]["thesis"], trips[0])
    assert "scalp" in line and "held 600s (over the limit)" in line and "exit at or past target" in line and "exit inside invalidation" in line
    text = j.render_memory(trips)
    assert "Thesis against what happened" in text and "scalp; target 7710" in text
    assert "REVERSAL" not in text


def test_memory_without_a_thesis_has_no_thesis_section():
    trips = j.attach_context(j.round_trips(j.fill_rows([raw(10, 0, 7700), raw(11, 1, 7701, pnl=4.0)])), {})
    assert "Thesis against" not in j.render_memory(trips)


def test_write_memory_reads_the_thesis_file(tmp_path, monkeypatch):
    led = tmp_path / "t.jsonl"
    led.write_text("\n".join(json.dumps(j.fill_row(r)) for r in [raw(10, 0, 7700, ts="2026-10-29T14:10:00+00:00"),
                                                                raw(11, 1, 7712, pnl=60.0, ts="2026-10-29T14:20:00+00:00")]) + "\n")
    tf = tmp_path / "th.jsonl"
    th.Store(tf).add(th.clean(card(), now_ms=ms(14, 9)))
    monkeypatch.setenv("PX_THESIS_FILE", str(tf))
    monkeypatch.setenv("PX_MEMORY_DIR", str(tmp_path / "mem"))
    j.write_memory(tmp_path, ledger_path=led, bias_path=tmp_path / "none")
    assert "Thesis against what happened" in (tmp_path / "mem" / "trades.md").read_text()


# ---- routes --------------------------------------------------------------------------------------------------------------

def mk(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_THESIS_FILE", str(tmp_path / "th.jsonl"))
    fake = FakeClient()
    return create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                      user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False)


def hpost(c, tok, body, **hdr):
    h = {"origin": "http://testserver", "x-px-csrf": tok, "content-type": "application/json"}
    h.update(hdr)
    return c.post("/api/thesis", json=body, headers={k: v for k, v in h.items() if v is not None})


def test_post_is_gated_saved_and_read_back(tmp_path, monkeypatch):
    app = mk(tmp_path, monkeypatch)
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, tok, card(), origin=None).status_code == 403
        assert hpost(c, tok, card(), origin="http://evil.example").status_code == 403
        assert hpost(c, "bad", card()).status_code == 403
        assert c.post("/api/thesis", content="a=1", headers={"origin": "http://testserver", "x-px-csrf": tok,
                                                              "content-type": "text/plain"}).status_code == 415
        assert hpost(c, tok, card(type="NOPE")).status_code == 400
        assert not (tmp_path / "th.jsonl").exists()
        r = hpost(c, tok, card())
        assert r.status_code == 200 and r.json()["thesis"]["side"] == "LONG" and r.json()["facts"] == []
        g = c.get("/api/thesis", params={"contract": MES}).json()
        assert g["thesis"]["target"] == 7710.0 and g["types"] == list(th.TYPES)
        assert c.get("/api/thesis", params={"contract": "bad"}).status_code == 400
        assert c.get("/api/thesis", params={"contract": "CON.F.US.MNQ.Z26"}).json()["thesis"] is None
    assert len((tmp_path / "th.jsonl").read_text().splitlines()) == 1


def test_a_broken_invalidation_shows_in_the_route(tmp_path, monkeypatch):
    app = mk(tmp_path, monkeypatch)
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        hpost(c, tok, card())
        app.state.live.quotes[MES] = {"lastPrice": 7690.0}
        g = c.get("/api/thesis", params={"contract": MES}).json()
        assert g["checks"]["invalidation_broken"] is True and g["facts"] == ["Price is through your invalidation 7695."]


def test_record_only_has_no_thesis_route(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_RECORD_ONLY", "1")
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"), allowed_hosts={"testserver"}, connect=False)
    with TestClient(app) as c:
        assert c.get("/api/thesis", params={"contract": MES}).status_code == 404
        assert c.post("/api/thesis", json=card()).status_code in (404, 405)


# ---- pins ----------------------------------------------------------------------------------------------------------------

def test_thesis_module_cannot_reach_the_order_side():
    src = (ROOT / "projectx_desk" / "thesis.py").read_text()
    tree = ast.parse(src)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if re.search(r"guard|client|hub|mcp|app|state", m)}, imported
    assert not re.search(r"/api/Order|place_order|\.write\(|\bplace\(|\bpreview\(", src)


def test_thesis_js_posts_only_to_thesis_and_has_no_order_words():
    js = (STATIC / "thesis.js").read_text()
    assert re.findall(r"""fetch\(\s*'/api/thesis'\s*,\s*\{[^}]*method:\s*'POST'""", js)
    assert set(re.findall(r"""fetch\(\s*['"]([^'"?]+)""", js)) == {"/api/csrf", "/api/thesis", "/api/respect"}
    assert not re.search(r"/api/(place|preview|flatten|stop|halt|reconcile|intent|readiness)", js, re.I)
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
