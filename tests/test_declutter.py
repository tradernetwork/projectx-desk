"""Chart declutter logic (static/declutter.js, pure, node) plus the wiring pins that keep it display only."""
import json
import pathlib
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "projectx_desk" / "static"
JS = (STATIC / "desk.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
DC = (STATIC / "declutter.js").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def js(expr, payload=None):
    code = f"const T=require(process.argv[1]);const d=JSON.parse(require('fs').readFileSync(0,'utf8'));console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code, str(STATIC / "declutter.js")], input=json.dumps(payload or {}), capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def wall(price, rel=0.5, kind="PUT", name="wall"):
    return {"price": price, "rel": rel, "kind": kind, "name": name}


LEVELS = [wall(7710, 1.0, None, "pin"), wall(7700, None, None, "flip"), wall(7760), wall(7740), wall(7720), wall(7690), wall(7680), wall(7650), wall(7600)]


@needs_node
def test_gex_keeps_flip_pin_and_the_two_nearest_walls_each_side():
    got = js("T.gexPick(d.l, d.p, false).map(x => [x.role, x.lv.price])", {"l": LEVELS, "p": 7705})
    assert [r for r, _ in got].count("flip") == 1 and [r for r, _ in got].count("pin") == 1
    assert sorted(p for r, p in got if r == "wall") == [7680, 7690, 7720, 7740]      # 2 above 7705, 2 below
    assert len(js("T.gexPick(d.l, d.p, true)", {"l": LEVELS, "p": 7705})) == len(LEVELS)      # More: every wall


@needs_node
def test_gex_label_is_short_and_the_gloss_rides_in_the_title():
    g = js("T.gexLabel(d.lv)", {"lv": wall(7704, 0.9, "PUT")})
    assert g["label"] == "wall 7704 BIG" and "speed up" in g["title"] and "PUT" not in g["label"]
    assert js("T.gexLabel(d.lv).label", {"lv": wall(7700, None, None, "flip")}) == "flip 7700"
    assert js("T.shortLabel(d.s)", {"s": "x" * 40}) == "x" * 22


@needs_node
def test_bands_only_for_the_top_two_by_size():
    picked = js("T.gexPick(d.l, d.p, false)", {"l": [wall(7720, 0.9), wall(7740, 0.5), wall(7690, 0.3), wall(7680, 0.7), wall(7700, None, None, "flip")], "p": 7705})
    got = js("[...T.gexBandPrices(d.pk, 2)]", {"pk": picked})
    assert sorted(got) == [7680, 7720] and 7700 not in got


@needs_node
def test_near_filter_is_two_atr_and_blind_without_an_atr():
    lines = [{"price": 100}, {"price": 104}, {"price": 99.5}]
    assert [x["price"] for x in js("T.nearFilter(d.l, 100, 1, 2)", {"l": lines})] == [100, 99.5]
    assert len(js("T.nearFilter(d.l, 100, null, 2)", {"l": lines})) == 3


@needs_node
def test_chip_cap_labels_four_but_never_removes_a_respected_line():
    lines = [{"price": 100 + i, "prio": 2, "label": f"r{i}"} for i in range(6)] + [{"price": 99, "prio": 5, "label": "auto"}]
    got = js("T.capChips(d.l, 100, 4)", {"l": lines})
    assert len([x for x in got if x["prio"] == 2]) == 6                              # every respected line survives
    assert len([x for x in got if x["label"]]) == 4 and not any(x["prio"] == 5 for x in got)      # labels capped at 4; the auto line lost the race
    few = js("T.capChips(d.l, 100, 4)", {"l": [{"price": 101, "prio": 2, "label": "a"}, {"price": 99, "prio": 5, "label": "b"}]})
    assert [x["label"] for x in few] == ["a", "b"]


@needs_node
def test_merge_keeps_the_higher_priority_and_adds_plus_tags():
    items = [{"key": "g", "src": "gex", "price": 100.2}, {"key": "r", "src": "respect", "price": 100.0}, {"key": "a", "src": "auto", "price": 100.1},
             {"key": "m", "src": "map", "price": 150}, {"key": "g2", "src": "gex", "price": 150.1}, {"key": "far", "src": "gex", "price": 120}]
    got = js("({drop: [...T.mergeLines(d.i, 1, 0.25).drop].sort(), plus: [...T.mergeLines(d.i, 1, 0.25).plus]})", {"i": items})
    assert got["drop"] == ["a", "g", "g2"]                                   # inside 0.25 ATR of a stronger source
    assert dict(got["plus"]) == {"r": ["+GEX"], "m": ["+GEX"]}               # respected wins over GEX and keeps its place
    assert js("T.withPlus('5m EMA 21', ['+GEX'])") == "5m EMA 21 +GEX"


@needs_node
def test_merge_never_drops_position_or_demotes_respected_to_a_weaker_source():
    items = [{"key": "stop", "src": "pos", "price": 100}, {"key": "stop2", "src": "pos", "price": 100.1}, {"key": "r", "src": "respect", "price": 100.05},
             {"key": "r2", "src": "respect", "price": 100.1}, {"key": "g", "src": "gex", "price": 100.05}]
    drop = js("[...T.mergeLines(d.i, 1, 0.25).drop].sort()", {"i": items})
    assert "stop" not in drop and "stop2" not in drop and "r" not in drop and "r2" not in drop      # position, stop and respected are never dropped (even when a stop sits on a respected level)
    assert "g" in drop
    assert js("[...T.mergeLines(d.i, null, 0.25).drop]", {"i": items}) == []   # no ATR, no merge
    only = [{"key": "r", "src": "respect", "price": 100}, {"key": "r2", "src": "respect", "price": 100.1}]
    assert js("[...T.mergeLines(d.i, 1, 0.25).drop]", {"i": only}) == []      # two respected levels are never merged away


@needs_node
def test_trend_tag_gate_is_1_5_atr_and_capped_at_four():
    assert js("(g => [g(100), g(101.4), g(103), g(99.9), g(100.1), g(100.2)])(T.tagGate(100, 1, 4, 1.5))") == [True, True, False, True, True, False]


@needs_node
def test_callout_helpers_round_age_and_drop_the_repeated_handle():
    assert [js("T.fmtAge(d.s)", {"s": s}) for s in (10, 300, 7200, 400000)] == ["now", "5m", "2h", "5d"]
    rows = [{"analyst": "Ace"}, {"analyst": "Ace"}]
    assert js("T.sharedHandle(d.r)", {"r": rows}) == "Ace" and js("T.sharedHandle(d.r)", {"r": rows + [{"analyst": "Bo"}]}) == ""
    assert js("T.sampleNote(19)") == "n=19 small sample" and js("T.sampleNote(40)") == "n=40"


def test_the_module_is_pure_and_the_page_loads_it_before_desk_js():
    for banned in ("fetch(", "document.", "localStorage", "XMLHttpRequest"):
        assert banned not in DC, banned
    assert HTML.index("/static/declutter.js") < HTML.index("/static/desk.js")


def test_toggles_persist_with_the_safe_wrapper_and_default_as_specified():
    assert "LS.get('px.gex', true) !== false" in JS and "LS.get('px.chartMore', false) === true" in JS
    assert "on: LS.get('px.tmap', false) === true" in JS                    # the map overlay defaults OFF, a saved value wins
    assert 'id="gex-toggle"' in HTML and 'id="more-toggle"' in HTML


def test_more_gates_the_secondary_overlays_and_the_cross_source_pass_exists():
    assert "orbDays().filter(d => CH.more || d.today)" in JS
    assert "CH.more && lv && Array.isArray(lv.events)" in JS
    assert "B > 0 && CH.more" in JS
    assert "R.on && CH.more && I.sel" in JS                                 # auto levels fold under Respect and need More
    plan = JS.split("function declutterPlan()")[1].split("function deskLines()")[0]
    assert "mergeLines" in plan and "capChips" in plan and "nearFilter" in plan


def test_lock_and_refusal_state_stay_outside_the_collapsed_math():
    assert 'id="rail-lock"' in HTML and "Locked for the session" in HTML
    before = HTML.split('<details id="size-more"')[0]
    assert 'id="size-refused"' in before and 'id="size-alert"' in before     # REFUSED stays on the headline
    folded = HTML.split('<details id="size-more"')[1].split("</details>")[0]
    assert 'id="size-why"' in folded and 'id="size-plan"' in folded and "size-refused" not in folded


def test_ai_panels_and_the_referral_link_survive():
    assert 'id="ai-digest"' in HTML and 'id="ai-state"' in HTML
    assert "tradermatrix.pro/?ref=MPHINANCE" in HTML
    for p in (STATIC / "index.html", STATIC / "desk.js", STATIC / "declutter.js"):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if "tradermatrix.pro" in ln:
                assert "ref=MPHINANCE" in ln, ln[:80]


def test_no_em_or_en_dashes_in_the_new_copy():
    assert "—" not in DC and "–" not in DC
    block = JS.split("function declutterPlan()")[1].split("function deskLines()")[0]
    assert "—" not in block and "–" not in block


def _c(id, kind="support", now=100, past=100, touches=3, rec=0.5, own=False):
    return {"id": id, "kind": kind, "now": now, "past": past, "touches": touches, "recency": rec, "own": own}


@needs_node
def test_trend_pick_drops_far_lines_and_near_parallel_duplicates_and_caps_at_four():
    cs = [_c("near", now=100.5), _c("far", now=110), _c("dup", now=100.6, past=100.2, touches=3), _c("res", "resistance", now=101, touches=4)]
    got = js("[...T.pickTrendLines(d.c, 100, 1)].sort()", {"c": cs})
    assert got == ["near", "res"]                                  # far is 10 ATR away; dup sits on near at both ends
    many = [_c(f"k{i}", now=100 + i * 0.4, past=100 + i * 0.4 + 2 * i, touches=3 + i) for i in range(8)]
    assert len(js("[...T.pickTrendLines(d.c, 100, 1, {far: 9})]", {"c": many})) == 4


@needs_node
def test_trend_pick_prefers_touches_and_recency_and_never_drops_an_own_line():
    cs = [_c("weak", "support", now=100.5, past=105, touches=3, rec=0.1), _c("strong", "support", now=100.6, past=106, touches=6, rec=0.9)]
    assert js("[...T.pickTrendLines(d.c, 100, 1, {cap: 1})]", {"c": cs}) == ["strong"]
    own = [_c("own", now=200, own=True), _c("x", now=100.2)]
    assert js("[...T.pickTrendLines(d.c, 100, 1, {cap: 0})]", {"c": own}) == ["own"]   # own is kept past the cap, even far
    assert js("[...T.pickTrendLines(d.c, null, null, {cap: 1})]", {"c": cs}) == ["strong"]       # blind: only the cap applies


def test_other_timeframe_tags_carry_no_timeframe_text():
    seg = JS[JS.index("const keep = PXDeclutter.pickTrendLines"):JS.index("if(!drew){applyRoom()")]
    assert "label: tf" not in seg and "dots: tf === '1m'" in seg
    assert "label: own" not in JS
