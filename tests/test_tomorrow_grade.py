"""The map grader. Parity with the TraderMatrix grader on 450 map/session pairs whose reference was produced by running the TypeScript
itself (tests/fixtures/tomorrow/grade_reference.json), plus the rules that keep a record honest."""
import json
import pathlib

import pytest

from projectx_desk import tomorrow_grade as G

REF = json.loads((pathlib.Path(__file__).resolve().parent / "fixtures" / "tomorrow" / "grade_reference.json").read_text(encoding="utf-8"))
EXACT = ("branch", "up_trigger", "down_trigger", "first_waypoint_reached", "close_vs_band", "hits", "total", "untested", "null_hits",
         "null_total", "wall_tagged", "summary")


def same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a == pytest.approx(b, abs=1e-9)
    return a == b


def test_the_reference_covers_every_branch_and_both_price_scales():
    branches = {c["grade"]["branch"] for c in REF["cases"]}
    assert branches == {"UP", "DOWN", "RANGE", "WHIPSAW"} and {m["ratio"] for m in REF["maps"]} == {1.0, 10.0788}
    assert len(REF["cases"]) == 450 and any("wicked" in c["grade"]["summary"] for c in REF["cases"])


def test_parity_with_the_typescript_grader_on_every_case():
    for i, c in enumerate(REF["cases"]):
        got = G.grade_map(REF["maps"][c["m"]]["map"], c["bar"])
        want = c["grade"]
        for k in EXACT:
            assert same(got[k], want[k]), (i, k, got[k], want[k])
        assert len(got["checks"]) == len(want["checks"]), i
        for gc, wc in zip(got["checks"], want["checks"]):
            for k in ("lvl", "side", "label", "dist", "thru", "tested", "ok", "wick_held"):
                assert same(gc[k], wc[k]), (i, k, gc, wc)


def test_baseline_step_scales_with_price_and_keeps_spy_on_five_dollars():
    assert [G.baseline_step(p) for p in (763.23, 7731.75, 29541.5, 4036.2)] == [5, 50, 200, 25]


def spy_map():
    fx = json.loads((pathlib.Path(__file__).resolve().parent / "fixtures" / "tomorrow" / "spy-2026-10-17.json").read_text(encoding="utf-8"))
    from projectx_desk import tomorrow as T
    ladder, share = T.survivor_ladder(fx["matrix"], fx["today"])
    g = fx["gex"]
    return T.build_tomorrow_map(g["spotPrice"], g["gammaFlipLevel"], g["totalGEX"], ladder, T.bars_from_history(fx["historical"]), share)


def test_a_flip_sitting_on_price_is_not_graded_and_the_put_wall_is_reported_never_scored():
    m = spy_map()
    assert G.extract_levels({**m, "flip_on_price": True})["flip"] is None and G.extract_levels(m)["flip"] == 776.41
    bar = {"o": m["spot"], "h": m["spot"] + 1, "l": 759.0, "c": 761.0}                      # tags the 760 put wall
    g = G.grade_map(m, bar)
    assert g["wall_tagged"] is True and all(c["label"].split()[-1] != "wall" for c in g["checks"]) and "tagged the 760 put wall" in g["summary"]


def test_a_level_price_never_went_near_is_untested_and_out_of_the_ratio():
    m = spy_map()
    quiet = {"o": m["spot"], "h": m["spot"] + 0.3, "l": m["spot"] - 0.1, "c": m["spot"] - 0.1}      # stays above the 763 trapdoor
    g = G.grade_map(m, quiet)
    assert g["branch"] == "RANGE" and g["untested"] >= 1 and g["total"] == len([c for c in g["checks"] if c["tested"]])
    assert g["checks"][-1]["side"] == "pin" and g["checks"][-1]["ok"] is True and g["hits"] == g["total"]


def row(session, hits, total, mv=1, gv=1, branch="UP"):
    return {"session": session, "map_version": mv, "grade_version": gv,
            "grade": {"branch": branch, "hits": hits, "total": total, "untested": 0, "summary": "s", "null_hits": 1, "null_total": 2}}


def test_no_rate_is_publishable_under_ten_sessions_and_only_one_version_pools():
    rows = [row(f"2026-10-{d:02d}", 2, 3) for d in range(1, 10)]
    r = G.record(rows)
    assert r["record"]["sessions"] == 9 and r["record"]["publishable"] is False and r["last_call"]["session"] == "2026-10-09"
    r10 = G.record(rows + [row("2026-10-12", 1, 2)])
    assert r10["record"]["publishable"] is True and (r10["record"]["hits"], r10["record"]["total"]) == (19, 29)
    mixed = G.record(rows + [row("2026-10-13", 3, 3, mv=2), row("2026-10-14", 1, 1, gv=2)])      # newer map version wins; other grade version ignored
    assert mixed["record"]["sessions"] == 1 and mixed["record"]["map_version"] == 2
    assert G.record([])["last_call"] is None and G.record([])["record"]["sessions"] == 0
