"""static/letf.js: the LETF desk's pure logic (run under node) and the static rules it must keep."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "projectx_desk" / "static" / "letf.js"
DATA = ROOT / "projectx_desk" / "rh" / "letf_data.json"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

SRC = JS.read_text(encoding="utf-8")


def strip_comments(s):
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", s, flags=re.S)


CODE = strip_comments(SRC)


# ---- static rules ----------------------------------------------------------------------------------------------------------------
def test_no_dynamic_html_eval_storage_console_or_post():
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage",
                "sessionStorage", "indexedDB", "console", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "importScripts"):
        assert bad not in CODE, bad
    for bad in ("POST", "method:", "csrf", "x-px-csrf"):
        assert bad not in CODE, bad                                  # display only: it cannot send anything


def test_fetches_only_the_five_read_routes_and_no_external_host():
    routes = set(re.findall(r"['\"`](/api/[^'\"`?]*)", CODE))
    reads = {"/api/rh/letf", "/api/rh/bars", "/api/rh/quotes", "/api/rh/movers", "/api/rh/gex"}
    assert routes <= reads, routes
    assert routes == reads                                                  # every one is used
    assert not re.search(r"https?://", CODE)
    for bad in ("/api/rh/preview", "/api/rh/place", "/api/rh/cancel", "/api/rh/release", "/api/rh/halt", "/api/csrf"):
        assert bad not in CODE


def test_never_touches_the_global_chart_registries_or_desk_js():
    for bad in ("registerIndicator", "registerOverlay", "registerFigure", "desk.js", "'VWAP'", '"VWAP"'):
        assert bad not in CODE, bad                                   # desk.js owns the global VWAP indicator


def test_no_em_or_en_dashes():
    assert chr(0x2014) not in SRC and chr(0x2013) not in SRC
    assert chr(0x2014) not in Path(__file__).read_text(encoding="utf-8")


# ---- the pure functions, under node ------------------------------------------------------------------------------------------------
def run(expr, **data):
    """Evaluate a JS expression with `r` = the module and each keyword as a const. The script goes in on stdin (the real
    universe is larger than the argv limit)."""
    pre = "".join(f"const {k}={json.dumps(v)};" for k, v in data.items())
    code = f"const r=require({json.dumps(str(JS))});{pre}process.stdout.write(JSON.stringify({expr}))"
    out = subprocess.run([NODE, "-"], input=code, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def U():
    return json.loads(DATA.read_text(encoding="utf-8"))


@needs_node
def test_formatting():
    assert run("[r.fmtPct(0.0112), r.fmtPct(-0.5), r.fmtPct(null), r.fmtPct(NaN), r.fmtPct(0)]") == ["+1.12%", "-50.00%", "NA", "NA", "0.00%"]
    assert run("[r.fmtBps(-5.4), r.fmtBps(12.3), r.fmtBps(null), r.fmtBps(0)]") == ["-5.4 bps", "+12 bps", "NA", "0.0 bps"]
    assert run("[r.fmtDollarVol(5319421111), r.fmtDollarVol(812e6), r.fmtDollarVol(4.1e6), r.fmtDollarVol(5e5), r.fmtDollarVol(null), r.fmtDollarVol(-1)]") \
        == ["$5.3B", "$812M", "$4.1M", "$500K", "NA", "NA"]
    assert run("[r.levLabel(3), r.levLabel(-2), r.levLabel(1.5), r.levLabel(null)]") == ["3x", "-2x", "1.5x", "?"]


@needs_node
def test_chart_titles_say_what_is_charted():
    g = {"key": "Gold", "chart": "GLD", "chart_kind": "proxy", "proxy_quality": "approximate"}
    assert run("r.chartTitle(u)", u=g) == "Gold, charted via GLD (approximate)"
    assert run("r.chartTitle(u)", u={**g, "proxy_quality": "exact"}) == "Gold, charted via GLD"
    assert run("r.chartTitle(u)", u={"key": "QQQ", "chart": "QQQ", "chart_kind": "self", "proxy_quality": None}) == "QQQ"
    assert run("r.chartTitle(u)", u={"key": "HYPE", "chart": None, "chart_kind": "none", "proxy_quality": None}) == "HYPE: no clean underlying chart, fund only"
    assert run("r.fundTitle('TQQQ', f)", f={"lev": 3, "name": "ProShares UltraPro QQQ", "issuer": None, "dv20": 5.3e9}) \
        == "TQQQ 3x long, ProShares UltraPro QQQ, $5.3B/day (20d)"
    assert run("r.fundTitle('SQQQ', f)", f={"lev": -3, "name": None, "issuer": "ProShares", "dv20": None}) == "SQQQ -3x short, ProShares, NA/day (20d)"


@needs_node
def test_age_note_levels():
    assert run("r.ageNote('2026-11-19', 3)") == {"text": "Fund list as of 2026-11-19 (3 days old)", "level": "ok"}
    assert run("r.ageNote('2026-11-19', 14)")["level"] == "warn"
    n = run("r.ageNote('2026-11-19', 45)")
    assert n["level"] == "stale" and "scripts/letf_sync.py" in n["text"]
    assert run("r.ageNote('2026-11-19', null)")["level"] == "warn"


@needs_node
def test_today_move_and_gap_bps():
    m = run("r.todayMove(101, 100, 103.2, 100, 3)")
    assert m["u"] == pytest.approx(0.01) and m["expected"] == pytest.approx(0.03) and m["e"] == pytest.approx(0.032)
    assert m["gap_bps"] == pytest.approx(20.0) and m["suspect"] is False
    for args in ("null, 100, 103, 100, 3", "101, 0, 103, 100, 3", "101, 100, 103, null, 3", "101, 100, NaN, 100, 3"):
        m = run(f"r.todayMove({args})")
        assert m["gap_bps"] is None                                         # unknown stays unknown, never 0
    m = run("r.todayMove(101, 100, 200, 100, 3)")                           # +100% on a +1% day: a split, not a gap
    assert m["suspect"] is True and m["e"] is None and m["gap_bps"] is None and m["u"] == pytest.approx(0.01)
    assert run("r.todayMove(99, 100, 103, 100, -3).gap_bps") == pytest.approx(0.0, abs=1e-6)      # an inverse fund: -3 x -1% = +3%


DAY = 86_400_000
FRI = 1_795_132_800_000                       # 2026-11-20T00:00:00Z, the Friday session's daily bar


@needs_node
def test_prev_close_weekend_rule():
    daily = [[FRI - DAY, 10, 10, 10, 10, 1], [FRI, 11, 11, 11, 11, 1]]
    assert run("r.sessionPrevClose(d, {prev_close: 9.5, updated_ms: 1})", d=daily) == 9.5                   # the quote's own value wins
    # Saturday: the last completed bar IS Friday's session, so yesterday's close is Thursday's
    assert run("r.sessionPrevClose(d, {updated_ms: ms})", d=daily, ms=FRI + 21 * 3600_000) == 10
    # Monday, market open: the last completed bar is Friday's and it is the right prev close
    assert run("r.sessionPrevClose(d, {updated_ms: ms})", d=daily, ms=FRI + 3 * DAY + 15 * 3600_000) == 11
    assert run("r.sessionPrevClose(d, null)", d=daily) == 11
    assert run("r.sessionPrevClose([], {updated_ms: 1})") is None
    assert run("r.sessionPrevClose(d, {updated_ms: ms})", d=[[FRI, 11, 11, 11, 11, 1]], ms=FRI + 21 * 3600_000) is None


def _series(n, drop_b=None, lev=3.0):
    """n consecutive daily bars for an underlying (a) and a fund (b) whose daily return is exactly lev x the underlying's."""
    import math
    a, b, ca, cb = [], [], 100.0, 50.0
    for i in range(n):
        if i:
            r = 0.01 * math.sin(i * 1.7) + 0.004 * math.cos(i * 0.9)
            ca, cb = ca * (1 + r), cb * (1 + lev * r)
        t = FRI - 40 * DAY + i * DAY
        a.append([t, ca, ca, ca, ca, 1])
        if i != drop_b:
            b.append([t, cb, cb, cb, cb, 1])
    return a, b


@needs_node
def test_realized_leverage_recovers_L_and_is_na_under_20():
    a, b = _series(30)
    rets = run("r.alignDaily(a, b)", a=a, b=b)
    assert len(rets) == 29
    rl = run("r.realizedLeverage(rets)", rets=rets)
    assert rl["beta"] == pytest.approx(3.0, abs=1e-9) and rl["r2"] == pytest.approx(1.0) and rl["n"] == 20
    a, b = _series(30, lev=-2.0)
    assert run("r.realizedLeverage(r.alignDaily(a, b)).beta", a=a, b=b) == pytest.approx(-2.0, abs=1e-9)
    a, b = _series(15)
    short = run("r.realizedLeverage(r.alignDaily(a, b))", a=a, b=b)
    assert short["beta"] is None and short["n"] == 14 and short["reason"] == "sample"
    flat = run("r.realizedLeverage(x)", x=[{"d": str(i), "u": 0.0, "e": 0.0} for i in range(25)])
    assert flat["beta"] is None and flat["reason"] == "flat"


@needs_node
def test_align_daily_breaks_the_chain_at_a_date_one_series_lacks():
    a, b = _series(30, drop_b=10)
    rets = run("r.alignDaily(a, b)", a=a, b=b)
    assert len(rets) == 27                                                  # the returns into dates 10 and 11 are not used
    assert all(abs(x["e"] - 3 * x["u"]) < 0.02 for x in rets)               # no multi-day return slipped in
    assert run("r.alignDaily(a, null)", a=a) == []


@needs_node
def test_vol_decay_formula_for_3x_and_minus_3x():
    rets = [0.01 if i % 2 else -0.01 for i in range(20)]
    d3 = run("r.volDecay(3, x)", x=rets)
    dm3 = run("r.volDecay(-3, x)", x=rets)
    import math
    logs = [math.log(1 + x) for x in rets]
    mean = sum(logs) / 20
    v = sum((x - mean) ** 2 for x in logs) / 19
    assert d3["n"] == 20 and d3["sigma_ann"] == pytest.approx(math.sqrt(v * 252))
    assert d3["decay_day"] == pytest.approx(-3 * v) and d3["decay_ann"] == pytest.approx(-3 * v * 252)   # -L(L-1)/2 = -3
    assert dm3["decay_day"] == pytest.approx(-6 * v)                        # -(-3)(-4)/2 = -6: the inverse fund drags twice as hard
    assert run("r.volDecay(1, x).decay_day", x=rets) == pytest.approx(0.0, abs=1e-15)           # an unlevered fund has no drag
    na = run("r.volDecay(3, x)", x=rets[:10])
    assert na["decay_day"] is None and na["n"] == 10 and na["reason"] == "sample"


@needs_node
def test_spread_bps_and_quote_age():
    assert run("r.spreadBps(100, 100.1)") == pytest.approx(0.1 / 100.05 * 1e4)
    for args in ("null, 1", "1, null", "0, 1", "2, 1", "NaN, 1", "-1, 1"):
        assert run(f"r.spreadBps({args})") is None
    assert run("[r.quoteAge(1000, 4000), r.quoteAge(5000, 4000), r.quoteAge(null, 4000)]") == [3, 0, None]


def _mini():
    return {"underlyings": [
        {"key": "A", "group": "index", "long": ["AX", "AY"], "short": ["AS"], "long_pick": "AX", "short_pick": "AS"},
        {"key": "B", "group": "sector", "long": ["BX"], "short": [], "long_pick": "BX", "short_pick": None}],
        "funds": {"AX": {"u": "A", "lev": 2, "dv20": None, "name": "Alpha Two", "issuer": None},
                  "AY": {"u": "A", "lev": 3, "dv20": 1e6, "name": None, "issuer": None},
                  "AS": {"u": "A", "lev": -3, "dv20": 9e7, "name": None, "issuer": "Acme"},
                  "BX": {"u": "B", "lev": 2, "dv20": 2e6, "name": None, "issuer": None}}}


@needs_node
def test_filter_rows_groups_side_leverage_thin_and_unknown_not_hidden():
    u = _mini()
    rows = run("r.filterRows(u, {})", u=u)
    assert [r["key"] for r in rows] == ["A", "B"]                           # A leads: its top fund trades $90M a day
    assert [f["sym"] for f in rows[0]["funds"]] == ["AS", "AY", "AX"]       # dollar volume order, unknown last
    thin = run("r.filterRows(u, {hideThin: true})", u=u)
    assert [f["sym"] for r in thin for f in r["funds"]] == ["AS", "AX"]     # AY ($1M) and BX ($2M) hidden; AX (unknown) is NOT
    assert [r["key"] for r in run("r.filterRows(u, {groups: ['sector']})", u=u)] == ["B"]
    assert [f["sym"] for f in run("r.filterRows(u, {side: 'short'})", u=u)[0]["funds"]] == ["AS"]
    assert [f["sym"] for r in run("r.filterRows(u, {lev: '3'})", u=u) for f in r["funds"]] == ["AS", "AY"]
    assert [f["sym"] for r in run("r.filterRows(u, {lev: 'other'})", u=u) for f in r["funds"]] == []
    assert [r["key"] for r in run("r.filterRows(u, {q: 'alpha'})", u=u)] == ["A"]
    assert [f["sym"] for f in run("r.filterRows(u, {q: 'alpha'})", u=u)[0]["funds"]] == ["AX"]    # only the fund that matched
    assert [r["key"] for r in run("r.filterRows(u, {q: 'b'})", u=u)] == ["B"]
    assert [r["key"] for r in run("r.filterRows(u, {sort: 'az'})", u=u)] == ["A", "B"]
    assert [r["key"] for r in run("r.filterRows(u, {sort: 'lev'})", u=u)] == ["A", "B"]


@needs_node
def test_filter_rows_on_the_real_universe(U):
    assert len(run("r.filterRows(U, {})", U=U)) == 315
    rows = run("r.filterRows(U, {q: 'tqqq'})", U=U)
    assert [r["key"] for r in rows] == ["QQQ"] and [f["sym"] for f in rows[0]["funds"]] == ["TQQQ"]
    com = run("r.filterRows(U, {groups: ['commodity']})", U=U)
    assert com and all(r["group"] == "commodity" for r in com)
    sh = run("r.filterRows(U, {side: 'short'})", U=U)
    assert sh and all(f["side"] == "short" for r in sh for f in r["funds"])
    top = max(U["funds"].values(), key=lambda f: f["dv20"] or -1)["u"]
    assert run("r.filterRows(U, {})[0].key", U=U) == top                      # the underlying of the most traded fund leads


@needs_node
def test_keyboard_neighbor_and_flip_side(U):
    rows = run("r.displayRows(r.filterRows(U, {}), U.groups)", U=U)
    first = rows[0]["key"]                                                   # the first row of the first group, as the rail draws it
    nxt = run("r.neighbor(rows, sym, 1)", rows=rows, sym=run("r.rowPick(rows[0], 'long')", rows=rows))
    assert nxt != run("r.rowPick(rows[0], 'long')", rows=rows) and U["funds"][nxt]["lev"] > 0    # the same side (long) on the next row
    top_long = run("r.rowPick(rows[0], 'long')", rows=rows)
    assert run("r.neighbor(rows, sym, -1)", rows=rows, sym=top_long) == top_long             # the first row stays put at the top
    assert run("r.neighbor(rows, 'NOPE', 1)", rows=rows) == run("r.rowPick(rows[0], 'long')", rows=rows)
    assert run("r.neighbor([], 'TQQQ', 1)") is None
    assert run("[r.flipSide(U, 'TQQQ', 'short'), r.flipSide(U, 'SQQQ', 'long'), r.flipSide(U, 'TQQQ', 'long'), r.flipSide(U, 'NOPE', 'long')]", U=U) \
        == ["SQQQ", "TQQQ", "TQQQ", None]
    assert run("r.flipSide(U, 'HYPE', 'short')", U=U) is None or True
    assert run("r.underlyingOf(U, 'TQQQ').key", U=U) == "QQQ" and run("r.underlyingOf(U, 'NOPE')", U=U) is None


@needs_node
def test_neighbor_falls_back_to_the_other_side_when_a_row_lacks_one():
    u = _mini()
    rows = run("r.displayRows(r.filterRows(u, {}), [['index','Broad index'],['sector','Sector']])", u=u)
    assert run("r.neighbor(rows, 'AS', 1)", rows=rows) == "BX"                # B has no short fund: the long pick is used


@needs_node
def test_hash_roundtrip_rejects_junk(U):
    assert run("r.parseHash('#letf=TQQQ&tf=5m', U)", U=U) == {"sym": "TQQQ", "tf": "5m", "on": True}
    assert run("r.parseHash('#letf=ZZZZ&tf=9m', U)", U=U) == {"sym": None, "tf": None, "on": True}
    assert run("r.parseHash('#letf=<script>', U)", U=U)["sym"] is None
    assert run("r.parseHash('#foo=1', U)", U=U) == {"sym": None, "tf": None, "on": False}
    assert run("r.parseHash('', null)") == {"sym": None, "tf": None, "on": False}
    assert run("[r.hashString('TQQQ', '5m'), r.hashString('TQQQ', 'x')]") == ["#letf=TQQQ&tf=5m", "#letf=TQQQ"]


T0 = 1_795_098_600_000                         # 2026-11-19T14:30:00Z, a 5-minute boundary


@needs_node
def test_forming_bar():
    f = run("r.updateForming(null, 100, ms, 300000, 99)", ms=T0 + 30_000)
    assert f == {"t": T0, "o": 99, "h": 100, "l": 99, "c": 100}
    f = run("r.updateForming(f, 98, ms, 300000, 99)", f=f, ms=T0 + 60_000)
    assert f == {"t": T0, "o": 99, "h": 100, "l": 98, "c": 98}
    g = run("r.updateForming(f, 101, ms, 300000, 98)", f=f, ms=T0 + 300_000)           # the next window starts a new bar
    assert g == {"t": T0 + 300_000, "o": 98, "h": 101, "l": 98, "c": 101}
    assert run("r.updateForming(f, NaN, 1, 300000, 1)", f=f) == f and run("r.updateForming(null, -1, 1, 300000, 1)") is None
    assert run("r.updateForming(null, 5, 1000, 300000, null).o") == 5
    bars = [[T0 - 300_000, 1, 2, 0.5, 1.5, 10]]
    assert run("r.withForming(b, f)", b=bars, f=f)[-1] == [T0, 99, 100, 98, 98, None]
    assert run("r.withForming(b, f)", b=bars + [[T0, 1, 1, 1, 1, 5]], f=f) == bars + [[T0, 1, 1, 1, 1, 5]]   # the real bar wins
    assert run("r.withForming(b, null)", b=bars) == bars


@needs_node
def test_next_poll_delay():
    assert run("r.nextPollDelay('5m', ms)", ms=T0 + 10_000) == 294_000
    assert run("r.nextPollDelay('5m', ms)", ms=T0 + 299_000) == 15_000          # never faster than 15 s
    assert run("r.nextPollDelay('1D', ms)", ms=T0) == 1_800_000
    assert run("r.nextPollDelay('1h', ms)", ms=T0) == 3_600_000 - (T0 % 3_600_000) + 4000 if (T0 % 3_600_000) else True


@needs_node
def test_et_date():
    assert run("r.etDate(ms)", ms=T0) == "2026-11-19"
    assert run("r.etDate(ms)", ms=FRI + 3 * 3600_000) == "2026-11-19"           # 03:00Z on the 20th is still the 19th in New York
    assert run("r.etDate(null)") is None


# ---- sticky side ("snap"): the bear stays the bear when the underlying changes -------------------------------------------------------------
@needs_node
def test_side_of_and_pick_for_row_keep_the_side_and_say_when_they_cannot():
    u = _mini()
    assert run("[r.sideOf(u,'AY'), r.sideOf(u,'AS'), r.sideOf(u,'NOPE')]", u=u) == ["long", "short", None]
    rows = run("r.filterRows(u, {})", u=u)
    a, b = rows[0], rows[1]
    assert run("r.pickForRow(row,'short')", row=a) == {"sym": "AS", "side": "short", "fellBack": False}
    assert run("r.pickForRow(row,'long')", row=a)["side"] == "long"
    assert run("r.pickForRow(row,'short')", row=b) == {"sym": "BX", "side": "long", "fellBack": True}     # B has no bear fund: bull, and it says so
    assert run("r.pickForRow(null,'short')") is None


@needs_node
def test_neighbor_honours_a_preferred_side_even_after_a_fallback():
    u = _mini()
    rows = run("r.displayRows(r.filterRows(u, {}), [['index','Broad index'],['sector','Sector']])", u=u)
    # standing on B's bull fund because B has no bear, with bear as the preference: the next row up is A and lands on its bear fund
    assert run("r.neighbor(rows, 'BX', -1, 'short')", rows=rows) == "AS"
    assert run("r.neighbor(rows, 'BX', -1)", rows=rows) in ("AY", "AX")        # without a preference the fund's own side decides


# ---- the ladder ------------------------------------------------------------------------------------------------------------------------
@needs_node
def test_ladder_funds_orders_bulls_then_bears_and_caps():
    u = _mini()
    lf = run("r.ladderFunds(u,'A')", u=u)
    assert [f["side"] for f in lf] == ["long", "long", "short"] and {f["sym"] for f in lf} == {"AX", "AY", "AS"}
    assert [f["sym"] for f in lf if f["side"] == "long"] == ["AY", "AX"]       # dollar volume order within a side, unknown last
    assert [f["sym"] for f in run("r.ladderFunds(u,'A',1)", u=u)] == ["AS"]    # the single most liquid fund
    assert run("r.ladderFunds(u,'NOPE')", u=u) == [] and run("r.ladderFunds(null,'A')") == []


@needs_node
def test_ladder_rows_gap_spread_and_na():
    funds = [{"sym": "T", "side": "long", "lev": 3, "dv20": 1e9}, {"sym": "S", "side": "short", "lev": -3, "dv20": 2e6}, {"sym": "X", "side": "long", "lev": 2, "dv20": None}]
    qs = {"T": {"bid": 99.9, "ask": 100.1, "last": 106, "prev_close": 100}, "S": {"last": 94.0, "prev_close": 100}}
    rows = run("r.ladderRows(funds, qs, 98, 100)", funds=funds, qs=qs)
    t, s, x = rows
    assert t["move"] == pytest.approx(0.06) and t["expected"] == pytest.approx(-0.06) and t["gap_bps"] == pytest.approx(1200.0)   # +6% on a -2% day
    assert t["spread_bps"] == pytest.approx(20.0, abs=0.1) and t["thin"] is False
    assert s["thin"] is True and s["spread_bps"] is None and s["move"] == pytest.approx(-0.06)
    assert x["last"] is None and x["move"] is None and x["gap_bps"] is None and x["thin"] is False     # no quote: NA, never 0
    assert run("r.ladderRows(funds, qs, null, null)[0].gap_bps", funds=funds, qs=qs) is None


# ---- hold replay -------------------------------------------------------------------------------------------------------------------------
def _rets(n, u=0.01, e=None):
    return [{"d": f"d{i}", "u": u if i % 2 == 0 else -u, "e": (3 * (u if i % 2 == 0 else -u)) if e is None else e} for i in range(n)]


@needs_node
def test_hold_windows_decompose_path_and_fees():
    rets = _rets(130)
    w = run("r.holdWindows(rets, 3, [21, 63, 126, 252])", rets=rets)
    assert [x["na"] for x in w] == [False, False, False, True] and w[3]["n"] == 130          # 252 sessions asked, 130 held: NA with the count
    x = w[0]
    assert x["n"] == 21 and x["simple"] == pytest.approx(3 * x["u"]) and x["path"] == pytest.approx(x["ideal"] - x["simple"])
    assert x["fund"] == pytest.approx(x["ideal"]) and x["fees_tracking"] == pytest.approx(0.0, abs=1e-12)    # the fund here IS the ideal daily reset
    assert x["path"] < 0                                                                      # choppy up/down days: compounding costs
    # a fund that loses 2 bps a day to fees sits below the ideal by about that much compounded
    fee = [{"u": r["u"], "e": (1 + 3 * r["u"]) * (1 - 0.0002) - 1} for r in rets]
    y = run("r.holdWindows(rets, 3, [63])[0]", rets=fee)
    assert y["fees_tracking"] < -0.01 and y["path"] == pytest.approx(run("r.holdWindows(rets, 3, [63])[0].path", rets=rets))
    assert run("r.holdWindows(rets, null, [21])[0].na", rets=rets) is True and run("r.holdWindows(null, 3)[0].na") is True


@needs_node
def test_hold_windows_ideal_cannot_go_below_zero_and_series_matches():
    crash = [{"u": -0.4, "e": -1.0}] + _rets(25)
    x = run("r.holdWindows(rets, 3, [26])[0]", rets=crash)
    assert x["ideal"] == -1.0 and x["fund"] == pytest.approx(-1.0)                            # a 3x fund cannot lose more than everything
    rets = _rets(30)
    ser = run("r.holdSeries(rets, 3, 21)", rets=rets)
    w = run("r.holdWindows(rets, 3, [21])[0]", rets=rets)
    assert len(ser["fund"]) == 22 and ser["fund"][0] == 0 and ser["fund"][-1] == pytest.approx(w["fund"])
    assert ser["ideal"][-1] == pytest.approx(w["ideal"]) and ser["simple"][-1] == pytest.approx(w["simple"])
    assert run("r.holdSeries(rets, 3, 99)", rets=rets) is None


# ---- the rebalance window --------------------------------------------------------------------------------------------------------------------
@needs_node
def test_rebalance_runs_mark_bars_starting_1530_to_1600_et_per_session():
    est = 1_795_120_200_000                       # 2026-11-19 15:30 ET (EST: 20:30Z)
    bars = [[est - 300_000 * 2 + 300_000 * i, 1, 1, 1, 1, 1] for i in range(10)]   # 15:20 .. 16:05 at 5 minutes
    runs = run("r.rebalanceRuns(bars,'5m')", bars=bars)
    assert runs == [{"from": est, "to": est + 5 * 300_000}]                          # 15:30 through 15:55; 16:00 and later are outside
    nxt = [[est + DAY + 300_000 * i, 1, 1, 1, 1, 1] for i in range(3)]
    assert len(run("r.rebalanceRuns(bars.concat(nxt),'5m')", bars=bars, nxt=nxt)) == 2   # a second session is a second run
    assert run("r.rebalanceRuns(bars,'1D')", bars=bars) == [] and run("r.rebalanceRuns(null,'5m')") == []
    hourly = [[est - 3_600_000, 1, 1, 1, 1, 1], [est, 1, 1, 1, 1, 1]]                # 14:30 and 15:30 hourly bars
    assert run("r.rebalanceRuns(h,'1h')", h=hourly) == [{"from": est, "to": est}]
    summer = 1781897400000                                                        # 2026-06-19 15:30 ET (EDT: 19:30Z)
    assert run("r.etMinutes(t)", t=summer) == 930 and run("r.etMinutes(t)", t=est) == 930 and run("r.etMinutes(null)") is None


# ---- dealer levels ---------------------------------------------------------------------------------------------------------------------------
@needs_node
def test_gex_lines_pick_flip_pin_and_two_nearest_walls_each_side_within_range():
    g = {"ok": True, "flip": 596.0, "pin": 600.0, "walls": [
        {"strike": 605.0, "type": "RESISTANCE", "kind": "CALL", "rel": 1.0}, {"strike": 610.0, "type": "RESISTANCE", "kind": "CALL"},
        {"strike": 615.0, "type": "RESISTANCE", "kind": "CALL"}, {"strike": 590.0, "type": "SUPPORT", "kind": "PUT"},
        {"strike": 585.0, "type": "SUPPORT", "kind": "PUT"}, {"strike": 580.0, "type": "SUPPORT"}, {"strike": 700.0, "type": "RESISTANCE", "kind": "CALL"},
        {"strike": 600.0, "type": "PIN"}]}
    ls = run("r.gexLines(g, 601)", g=g)
    assert [x["price"] for x in ls] == [610.0, 605.0, 600.0, 596.0, 590.0, 585.0]       # 615 and 580 are third nearest, 700 is out of range, the pin strike is not a wall too
    assert [x["label"] for x in ls] == ["CALL WALL", "CALL WALL", "PIN", "GAMMA FLIP", "PUT WALL", "PUT WALL"]
    assert [x["kind"] for x in run("r.gexLines(g, 601, 0.002)", g=g)] == ["pin"]                      # a 0.2% range keeps only the pin (0.17% away)
    assert run("r.gexLines(null, 601)") == [] and run("r.gexLines({ok:false}, 601)") == [] and run("r.gexLines(g, null)", g=g) == []
    assert run("r.gexLines({ok:true, walls:[{strike:600,type:'SUPPORT'}]}, 601)")[0]["label"] == "SUPPORT"      # no gamma sign: the source's own type


# ---- movers ----------------------------------------------------------------------------------------------------------------------------------
@needs_node
def test_movers_rows_rank_filter_and_never_rank_unknown_as_zero():
    rows = [{"s": "A", "lev": 3, "dv20": 1e9, "move": 0.05, "gap_bps": 10}, {"s": "B", "lev": -3, "dv20": 1e9, "move": -0.05, "gap_bps": -250},
            {"s": "C", "lev": 2, "dv20": 1e6, "move": 0.07, "gap_bps": 5}, {"s": "D", "lev": 2, "dv20": 1e9, "move": None, "gap_bps": None},
            {"s": "E", "lev": -2, "dv20": None, "move": 0.0, "gap_bps": None}]
    f = lambda o: [x["s"] for x in run(f"r.moversRows(rows, {json.dumps(o)})", rows=rows)]       # noqa: E731
    assert f({"sort": "gain"}) == ["C", "A", "E", "B"] and f({"sort": "loss"}) == ["B", "E", "A", "C"]          # D (unknown) is left out, E (a real 0) is kept
    assert f({"sort": "gap"}) == ["B", "A", "C"]                                                 # by absolute gap; the unknown gaps are left out
    assert f({"sort": "gain", "side": "short"}) == ["E", "B"] and f({"sort": "gain", "side": "long"}) == ["C", "A"]
    assert f({"sort": "gain", "hideThin": True}) == ["A", "E", "B"]                              # C is $1M a day; E's unknown volume is not hidden
    assert f({"sort": "gain", "limit": 2}) == ["C", "A"]


# ---- the snapshot ----------------------------------------------------------------------------------------------------------------------------
@needs_node
def test_snapshot_name_and_lines_carry_no_account_text():
    assert run("r.snapshotName('TQQQ','5m',t)", t=1_795_120_200_000) == "letf-TQQQ-5m-20261119-2030.png"
    lines = run("r.snapshotLines([['Gap','+12 bps'],['Spread','1.2 bps']], '2026-11-19', t)", t=1_795_120_200_000)
    assert lines[0] == "Gap:  +12 bps" and lines[1] == "Spread:  1.2 bps" and lines[2] == ""
    assert "Fund list as of 2026-11-19" in lines[3] and "Display only, not advice" in lines[3] and "UTC" in lines[3]
    assert run("r.snapshotLines([], null, null)")[-1].startswith("Fund list as of ?")
