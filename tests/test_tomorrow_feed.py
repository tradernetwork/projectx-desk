"""Tomorrow's Map feed: source choice, payload cleaning, the futures reference and ratio, the conversion of every printed number, the
cache, and a fixed code for every way it can fail."""
import asyncio
import json
import pathlib
import types

import pytest

from projectx_desk import gex
from projectx_desk import tomorrow as T
from projectx_desk import tomorrow_feed as F

FX = json.loads((pathlib.Path(__file__).resolve().parent / "fixtures" / "tomorrow" / "spy-2026-10-17.json").read_text(encoding="utf-8"))
SNAP = "2026-10-16T20:01:00Z"
SNAP_EPOCH = gex._parse_epoch(SNAP)
MES, MCL = "CON.F.US.MES.Z26", "CON.F.US.MCL.Z26"
ENV = {"PX_GEX_KEY": "k" * 40}
REF = 7632.5


def payloads():
    g = {**FX["gex"], "lastUpdated": SNAP}
    m = {"snapshotTime": SNAP, "spotPrice": FX["gex"]["spotPrice"], "expirations": FX["matrix"]["expirations"], "rows": FX["matrix"]["rows"]}
    return g, m, FX["historical"]


class Client:
    def __init__(self, bars):
        self.bars_rows, self.calls = bars, 0

    async def bars(self, params):
        self.calls += 1
        self.last_params = params
        return self.bars_rows


def bar(epoch, close):
    from datetime import datetime, timezone
    return {"t": datetime.fromtimestamp(epoch - epoch % 300, timezone.utc).isoformat(), "o": close, "h": close, "l": close, "c": close, "v": 1}


def app(bars=None):
    return types.SimpleNamespace(state=types.SimpleNamespace(client=Client([bar(F.ref_epoch(SNAP_EPOCH), REF)] if bars is None else bars)))


def fake_fetch(counter=None, raise_=None):
    g, m, h = payloads()

    async def fetch(base, key, path):
        if counter is not None:
            counter.append(path)
        if raise_:
            raise raise_
        return g if path.endswith(("/SPY", "/QQQ", "/GLD")) else m if path.endswith("/matrix") else h
    return fetch


def run(coro):
    return asyncio.run(coro)


def test_source_symbol_is_the_etf_proxy_and_unsupported_roots_have_none():
    assert F.map_source("MES") == ("SPY", 10.0) and F.map_source("ES") == ("SPY", 10.0)
    assert F.map_source("MNQ")[0] == "QQQ" and F.map_source("MGC")[0] == "GLD"
    assert F.map_source("MCL") is None and F.map_source("NOPE") is None            # crude's proxy holds rolling futures: refused


def test_clean_inputs_keeps_numbers_and_rejects_garbage():
    g, m, h = payloads()
    c = F.clean_inputs(g, m, h)
    assert c["spot"] == 763.23 and c["flip"] == 776.41 and len(c["rows"]) == len(m["rows"]) and c["exps"] == m["expirations"]
    no_time = {k: v for k, v in g.items() if k != "lastUpdated"}
    for bad in ((None, m, h), (g, {**m, "expirations": ["2026-10-19", "<script>"]}, h), (g, {**m, "rows": m["rows"][:2]}, h),
                ({**g, "totalGEX": "x"}, m, h), (g, m, []), (no_time, {**m, "snapshotTime": "nope"}, h)):
        with pytest.raises(gex.GexError):
            F.clean_inputs(*bad)
    assert F.clean_inputs(g, {**m, "snapshotTime": "nope"}, h)["snap"] == SNAP_EPOCH      # falls back to the GEX payload's own timestamp
    rows = [{"strike": 1e9, "gex": [1] * 10}, {"strike": 763, "gex": [1] * 3}, *m["rows"][:5]]      # absurd strike, wrong-length row dropped
    assert len(F.clean_inputs(g, {**m, "rows": rows}, h)["rows"]) == 5


def test_every_number_in_the_text_is_in_futures_space_and_structure_matches_the_source_map():
    res = run(F.tomorrow_result(app(), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 3600))
    assert res["status"] == "OK" and res["source"] == "SPY" and res["tick"] == 0.25
    assert res["ratio"] == pytest.approx(REF / 763.23, rel=1e-6) and res["age_s"] == 3600
    mp = res["map"]
    assert mp["spot"] == REF and mp["flip"] == 7764.25 and mp["pin"] == 7630.25
    ladder, share = T.survivor_ladder(FX["matrix"], "2026-10-16")
    src = T.build_tomorrow_map(763.23, 776.41, FX["gex"]["totalGEX"], ladder, T.bars_from_history(FX["historical"]), share)
    for r, s in zip(mp["roads"], src["roads"]):                                    # same roads, same roles, same tail flags, same order
        assert [w["tag"].split(" ", 1)[1] for w in r["waypoints"]] == [w["tag"].split(" ", 1)[1] for w in s["waypoints"]]
        assert [w["tail"] for w in r["waypoints"]] == [w["tail"] for w in s["waypoints"]]
    (_, up_c, up_r), (_, dn_c, _), (_, el_c, _) = T.branches(mp)
    assert up_c == "it goes up through 7670.25" and "over 7764.25 dealers stop chasing" in up_r
    assert dn_c == "it goes below 7630.25" and el_c == "it holds between 7630.25 and 7670.25"
    assert not any(tok in json.dumps(mp) for tok in (" 767 ", " 763 ", "776.41"))   # no source-space number leaks into the futures map
    assert mp["typical_move"] == pytest.approx(11.01 * REF / 763.23, abs=0.2)


def test_cache_within_one_app_state_and_loop():
    calls, a = [], app()
    fetch = fake_fetch(calls)

    async def go():
        r1 = await F.tomorrow_result(a, MES, environ=ENV, fetch=fetch, now=SNAP_EPOCH + 3600)
        r2 = await F.tomorrow_result(a, MES, environ=ENV, fetch=fetch, now=SNAP_EPOCH + 3600)
        return r1, r2
    r1, r2 = run(go())
    assert r1 is r2 and len(calls) == 3 and a.state.client.calls == 1               # gex, matrix, historical once; one bars read


@pytest.mark.parametrize("kw,env,contract,status,code", [
    ({}, {}, MES, "NO_KEY", None),
    ({}, ENV, MCL, "UNAVAILABLE", "UNSUPPORTED"),
    ({}, ENV, "CON.F.US.ZZZ.Z26", "UNAVAILABLE", "CONTRACT"),
    ({"raise_": gex.GexError("AUTH")}, ENV, MES, "ERROR", "AUTH"),
    ({"raise_": gex.GexError("UPSTREAM_ERROR")}, ENV, MES, "ERROR", "UPSTREAM_ERROR"),
])
def test_every_failure_is_a_fixed_status_and_code(kw, env, contract, status, code):
    res = run(F.tomorrow_result(app(), contract, environ=env, fetch=fake_fetch(**kw), now=SNAP_EPOCH + 3600))
    assert (res["status"], res["code"], res["map"]) == (status, code, None)


def test_no_futures_reference_means_no_map_not_a_guessed_ratio():
    res = run(F.tomorrow_result(app(bars=[]), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 3600))
    assert (res["status"], res["code"]) == ("UNAVAILABLE", "NO_REF")
    wrong = [bar(F.ref_epoch(SNAP_EPOCH), 3000.0)]                                               # a futures price nowhere near 10x SPY
    res = run(F.tomorrow_result(app(bars=wrong), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 3600))
    assert (res["status"], res["code"]) == ("UNAVAILABLE", "CONVERT")


def test_a_map_older_than_a_trading_week_is_refused_and_a_future_snapshot_is_bad():
    old = run(F.tomorrow_result(app(), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 6 * 86400))
    assert (old["status"], old["code"]) == ("UNAVAILABLE", "STALE")
    fut = run(F.tomorrow_result(app(), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH - 7200))
    assert (fut["status"], fut["code"]) == ("ERROR", "BAD_PAYLOAD")


def test_the_route_is_a_get_with_a_validated_contract(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from projectx_desk import app as appmod
    from test_px_app_actions import make_app

    async def fake(app_, contract, **kw):
        return {"status": "OK", "contract": contract}
    monkeypatch.setattr(appmod.px_tomorrow, "tomorrow_result", fake)
    app, *_ = make_app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/tomorrow", params={"contract": MES}).json() == {"status": "OK", "contract": MES}
        assert c.get("/api/tomorrow", params={"contract": "bad id"}).status_code == 400
        assert c.post("/api/tomorrow", json={}, headers={"origin": "http://testserver"}).status_code in (403, 405)     # no write surface


def test_a_snapshot_taken_seconds_ago_uses_the_forming_bar():
    a = app()
    res = run(F.tomorrow_result(a, MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 30))
    assert res["status"] == "OK"
    assert a.state.client.last_params["includePartialBar"] is True               # excluding it made GLD (MGC) return NO_REF at the top of a bar
    assert a.state.client.last_params["unit"] == 2 and a.state.client.last_params["unitNumber"] == 5


@pytest.mark.parametrize("status,body,code", [(401, "{}", "AUTH"), (403, "{}", "AUTH"), (429, "{}", "RATE_LIMIT"), (500, "{}", "UPSTREAM_ERROR"),
                                              (200, "not json", "UPSTREAM_ERROR"), (200, json.dumps({"success": False}), "UPSTREAM_ERROR")])
def test_http_failures_become_fixed_codes(monkeypatch, status, body, code):
    import httpx
    real = httpx.AsyncClient
    monkeypatch.setattr(F.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(status, text=body)), **kw))
    with pytest.raises(gex.GexError) as e:
        run(F.fetch_json("https://example.test", "k" * 40, "/gex/SPY"))
    assert e.value.code == code


def test_the_data_wrapper_is_unwrapped_and_a_huge_body_is_refused(monkeypatch):
    import httpx
    real = httpx.AsyncClient
    monkeypatch.setattr(F.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"success": True, "data": {"a": 1}})), **kw))
    assert run(F.fetch_json("https://example.test", "k" * 40, "/gex/SPY")) == {"a": 1}
    big = "x" * (gex.MAX_BODY + 10)
    monkeypatch.setattr(F.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=big)), **kw))
    with pytest.raises(gex.GexError) as e:
        run(F.fetch_json("https://example.test", "k" * 40, "/gex/SPY"))
    assert e.value.code == "UPSTREAM_ERROR"


@pytest.mark.parametrize("name", ["spy-2026-10-17", "spy-2026-10-17-brake-ceiling", "spy-2026-10-17-shelf-cushion"])
@pytest.mark.parametrize("ratio,tick", [(10.0788, 0.25), (41.3579, 0.25), (10.9477, 0.1)])
def test_no_number_in_a_futures_sentence_is_in_the_wrong_space(name, ratio, tick):
    """Regression: a flip-led trigger was converted twice (78337.5 for 7833.75). Every number a sentence prints must sit near futures spot."""
    import re
    fx = json.loads((pathlib.Path(__file__).resolve().parent / "fixtures" / "tomorrow" / f"{name}.json").read_text(encoding="utf-8"))
    ladder, share = T.survivor_ladder(fx["matrix"], fx["today"])
    g = fx["gex"]
    src = T.build_tomorrow_map(g["spotPrice"], g["gammaFlipLevel"], g["totalGEX"], ladder, T.bars_from_history(fx["historical"]), share)
    mp = T.build_tomorrow_map(g["spotPrice"], g["gammaFlipLevel"], g["totalGEX"], ladder, T.bars_from_history(fx["historical"]), share, ratio=ratio, tick=tick)
    spot = mp["spot"]
    text = " ".join(c + " " + r for _, c, r in T.branches(mp)) + " " + " ".join(w["tag"] for rd in mp["roads"] for w in rd["waypoints"])
    for tok in re.findall(r"\d+(?:\.\d+)?", text):
        assert 0.9 * spot <= float(tok) <= 1.1 * spot, (tok, spot, text)
    for (_, sc, _), (_, fc, _) in zip(T.branches(src), T.branches(mp)):             # the same sentence shapes, only the numbers differ
        assert re.sub(r"\d+(?:\.\d+)?", "N", sc) == re.sub(r"\d+(?:\.\d+)?", "N", fc)
    for sr, fr in zip(src["roads"], mp["roads"]):
        assert re.sub(r"\d+(?:\.\d+)?", "N", sr["rule"]).replace("N.00", "N") == re.sub(r"\d+(?:\.\d+)?", "N", fr["rule"]).replace("N.00", "N")


def test_survivors_use_the_snapshots_own_date_not_the_clock_of_a_late_retry():
    late = SNAP_EPOCH + 10 * 3600                                                    # a retry hours later, past midnight in New York
    res = run(F.tomorrow_result(app(), MES, environ=ENV, fetch=fake_fetch(), now=late))
    assert res["status"] == "OK" and res["today"] == "2026-10-16" and res["after_hours"] is False       # 15:01 CT is inside the cash session


def test_a_snapshot_outside_the_cash_session_is_flagged_after_hours(monkeypatch):
    g, m, h = payloads()
    evening = "2026-10-16T01:30:00Z"                                                 # 20:30 CT the evening before
    snap = gex._parse_epoch(evening)

    async def fetch(base, key, path):
        return {**g, "lastUpdated": evening} if path.endswith("/SPY") else {**m, "snapshotTime": evening} if path.endswith("/matrix") else h
    a = app(bars=[bar(F.ref_epoch(snap), REF)])                                      # an evening snapshot pairs with the cash close, not its own bar
    res = run(F.tomorrow_result(a, MES, environ=ENV, fetch=fetch, now=snap + 600))
    assert res["status"] == "OK" and res["after_hours"] is True


def test_an_early_refusal_is_reported_with_its_real_code_not_loading_forever(monkeypatch):
    for k in ("PX_GEX_KEY", "TD_API_KEY", "PX_GEX_URL"):
        monkeypatch.delenv(k, raising=False)
    a = app()

    async def go():
        first = await F.ai_block(a, MES)
        await asyncio.gather(*list(a.state.tmap_kick_tasks))
        return first, await F.ai_block(a, MES), len(a.state.tmap_kick_tasks)
    first, second, held = run(go())
    assert first["code"] == "LOADING" and second == {"status": "NO_KEY", "code": None} and held == 0       # the task was kept while running and dropped when done


def test_ref_epoch_is_the_snapshot_in_session_and_the_cash_close_otherwise():
    from datetime import datetime
    from projectx_desk.tradeday import CT
    ct = lambda y, mo, d, h, mi: datetime(y, mo, d, h, mi, tzinfo=CT).timestamp()
    close = lambda y, mo, d: datetime(y, mo, d, 14, 59, 59, tzinfo=CT).timestamp()
    mid = ct(2026, 10, 15, 10, 30)                                           # Thursday mid-session: the snapshot's own moment
    assert F.ref_epoch(mid) == mid
    assert F.ref_epoch(ct(2026, 10, 15, 15, 1)) == close(2026, 10, 15)       # just after the cash close: that day's 15:00
    assert F.ref_epoch(ct(2026, 10, 15, 21, 0)) == close(2026, 10, 15)       # evening
    assert F.ref_epoch(ct(2026, 10, 16, 6, 0)) == close(2026, 10, 15)        # before the open: the day before
    assert F.ref_epoch(ct(2026, 10, 17, 12, 0)) == close(2026, 10, 16)       # Saturday: Friday's close
    assert F.ref_epoch(ct(2026, 10, 19, 7, 0)) == close(2026, 10, 16)        # Monday pre-market: Friday's close


def test_an_after_hours_snapshot_scales_by_the_cash_close_not_the_overnight_futures_bar():
    """SNAP is 15:01 CT. A futures bar at the snapshot (the overnight price, 3% away) must be ignored; the 14:55 bar decides the ratio."""
    overnight = bar(SNAP_EPOCH, REF * 1.03)
    close_bar = bar(F.ref_epoch(SNAP_EPOCH), REF)
    res = run(F.tomorrow_result(app(bars=[overnight, close_bar]), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 3600))
    assert res["status"] == "OK" and abs(res["ratio"] - REF / FX["gex"]["spotPrice"]) < 1e-5
    only_overnight = run(F.tomorrow_result(app(bars=[overnight]), MES, environ=ENV, fetch=fake_fetch(), now=SNAP_EPOCH + 3600))
    assert (only_overnight["status"], only_overnight["code"]) == ("UNAVAILABLE", "NO_REF")        # never falls back to the overnight bar
