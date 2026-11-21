"""scripts/probe_report.py: the read-only table for docs/SIM_PROBE.md. Not part of the desk.

Every fixture here is INVENTED: ticket hashes, order ids, account ids and tags are made up and match no real account.
"""
import ast
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_report.py"
spec = importlib.util.spec_from_file_location("probe_report", SCRIPT)
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

ACCOUNT = 987654321          # invented
H_LONG, H_SHORT, H_TE, H_1L, H_S5 = "aaaa0001", "bbbb0002", "cccc0003", "dddd0004", "eeee0005"


def ev(event, **kw):
    return {"ts": "2026-11-22T17:05:00-06:00", "event": event, **kw}


def put(path, recs, extra_lines=()):
    lines = [json.dumps(r) for r in recs] + list(extra_lines)
    path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    return path


def table(tmp_path, capsys, recs=(), orders=None, extra=(), args=()):
    a = put(tmp_path / "audit.jsonl", recs, extra)
    argv = ["--audit", str(a)]
    if orders is not None:
        o = tmp_path / "orders.json"
        o.write_text(json.dumps(orders), encoding="utf-8")
        argv += ["--orders", str(o)]
    rc = pr.main(argv + list(args))
    out = capsys.readouterr().out
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] in ("PASS", "FAIL", "UNKNOWN"):
            rows[parts[0]] = (parts[1], line)
    return rc, rows, out


def entry(h, side, stop="ACKED", extra=()):
    return [ev("preview", ticket=h, origin="ui", root="MES", side=side, size=1, sha8=h), ev("place", ticket=h, origin="ui", root="MES",
            side=side, size=1, sha8=h), ev("ack", ticket=h, state="PLACED", code="0"), *extra,
            ev("stop", ticket=h, root="MES", size=1, state="SENDING", via="hub"), ev("stop", ticket=h, state=stop)]


# ---- UNKNOWN on empty and garbled input -----------------------------------------------------------------------------------

def test_empty_audit_prints_a_table_of_unknowns_and_exits_0(tmp_path, capsys):
    rc, rows, out = table(tmp_path, capsys)
    assert rc == 0 and rows and all(v == "UNKNOWN" for v, _ in rows.values())
    assert "PASS n=0, FAIL n=0" in out


def test_garbled_lines_are_skipped_and_counted_never_read_as_a_pass(tmp_path, capsys):
    rc, rows, out = table(tmp_path, capsys, extra=['{"half":', "not json", "[1,2]", '{"event": 5}', '"str"'])
    assert rc == 0 and "lines n=5, parsed n=0, skipped (garbled) n=5" in out
    assert all(v == "UNKNOWN" for v, _ in rows.values())


def test_garbled_lines_beside_good_ones_do_not_hide_them(tmp_path, capsys):
    rc, rows, out = table(tmp_path, capsys, entry(H_LONG, "long"), extra=["{{{"])
    assert rc == 0 and rows["S2L"][0] == "PASS" and "skipped (garbled) n=1" in out


# ---- bad input is exit 2 ------------------------------------------------------------------------------------------------------

def test_missing_audit_is_exit_2(tmp_path, capsys):
    assert pr.main(["--audit", str(tmp_path / "none.jsonl")]) == 2
    assert "bad input" in capsys.readouterr().err


def test_orders_that_are_not_order_search_json_are_exit_2(tmp_path, capsys):
    a = put(tmp_path / "a.jsonl", [])
    for body in ("not json", json.dumps({"success": True}), json.dumps({"orders": "x"}), json.dumps(5)):
        (tmp_path / "o.json").write_text(body, encoding="utf-8")
        assert pr.main(["--audit", str(a), "--orders", str(tmp_path / "o.json")]) == 2
    assert pr.main(["--audit", str(a), "--orders", str(tmp_path / "missing.json")]) == 2


def test_a_malformed_binding_or_tick_is_exit_2(tmp_path):
    a = str(put(tmp_path / "a.jsonl", []))
    for bad in (["--ticket", "S11"], ["--ticket", "S99=aaaa0001"], ["--ticket", "S11=xyz"], ["--ticket", "S11=AAAA00011"],
                ["--tick", "0"], ["--tick", "nan"]):
        assert pr.main(["--audit", a, *bad]) == 2, bad


# ---- each verdict --------------------------------------------------------------------------------------------------------------

def test_s1_logout_cleared_passes_relatched_fails_absent_is_unknown(tmp_path, capsys):
    _, rows, _ = table(tmp_path, capsys, [ev("halt", code="gateway_logout"), ev("halt", code="cleared")])
    assert rows["S1"][0] == "PASS" and rows["X0"][0] == "PASS"
    _, rows, _ = table(tmp_path, capsys, [ev("halt", code="gateway_logout"), ev("halt", code="cleared"), ev("halt", code="gateway_logout")])
    assert rows["S1"][0] == "FAIL" and "gateway_logout n=2" in rows["S1"][1]
    _, rows, _ = table(tmp_path, capsys, [ev("halt", code="manual")])
    assert rows["S1"][0] == "UNKNOWN"


def test_s2_entries_pass_per_side_and_fail_on_a_failed_stop(tmp_path, capsys):
    _, rows, _ = table(tmp_path, capsys, entry(H_LONG, "long") + entry(H_SHORT, "short"))
    assert rows["S2L"][0] == "PASS" and rows["S2S"][0] == "PASS"
    _, rows, _ = table(tmp_path, capsys, entry(H_LONG, "long", stop="FAILED"))
    assert rows["S2L"][0] == "FAIL" and rows["S2S"][0] == "UNKNOWN"
    _, rows, _ = table(tmp_path, capsys, [ev("place", ticket=H_LONG, side="long"), ev("unknown", ticket=H_LONG, state="UNKNOWN", code="transport")])
    assert rows["S2L"][0] == "FAIL" and rows["X1"][0] == "FAIL"


def test_s2_placed_but_no_stop_yet_is_unknown_not_pass(tmp_path, capsys):
    _, rows, _ = table(tmp_path, capsys, [ev("place", ticket=H_LONG, side="long"), ev("ack", ticket=H_LONG, state="PLACED", code="0")])
    assert rows["S2L"][0] == "UNKNOWN"


def test_s2f_flatten(tmp_path, capsys):
    _, rows, _ = table(tmp_path, capsys, [ev("flatten", origin="ui", state="FLAT", size=1)])
    assert rows["S2F"][0] == "PASS"
    _, rows, _ = table(tmp_path, capsys, [ev("flatten", origin="ui", state="FLAT"), ev("flatten", origin="ui", state="NOT_FLAT")])
    assert rows["S2F"][0] == "FAIL"


def test_s3_and_s4_cancels(tmp_path, capsys):
    recs = [ev("cancel", ticket=H_LONG, origin="ui", state="OK", code="user"), ev("cancel", ticket=H_SHORT, origin="ttl", state="OK", code="limit_ttl")]
    _, rows, _ = table(tmp_path, capsys, recs)
    assert rows["S3"][0] == "PASS" and rows["S4"][0] == "PASS"
    _, rows, _ = table(tmp_path, capsys, [ev("cancel", ticket=H_LONG, state="UNKNOWN", code="user"), ev("halt", code="cancel_unknown")])
    assert rows["S3"][0] == "FAIL" and rows["X0"][0] == "FAIL" and rows["X1"][0] == "FAIL"
    _, rows, _ = table(tmp_path, capsys, [ev("cancel", ticket=H_LONG, state="FAILED", code="limit_ttl")])
    assert rows["S4"][0] == "FAIL"


def test_desk_side_refusals(tmp_path, capsys):
    recs = [ev("reject", origin="ui", check="limit_rests"), ev("reject", origin="ui", check="stop_clear_of_market"),
            ev("cancel", ticket=H_LONG, origin="ui", state="REFUSED", code="has_fill")]
    _, rows, _ = table(tmp_path, capsys, recs)
    assert rows["S5a"][0] == rows["S6a"][0] == rows["S7a"][0] == "PASS"
    assert rows["S6"][0] == rows["S7"][0] == rows["S13"][0] == rows["S14"][0] == "UNKNOWN"     # by hand, never inferred


def test_s5_needs_a_binding_and_reports_either_answer(tmp_path, capsys):
    acked = [ev("place", ticket=H_S5, side="long"), ev("ack", ticket=H_S5, state="PLACED", code="0")]
    _, rows, _ = table(tmp_path, capsys, acked)
    assert rows["S5"][0] == "UNKNOWN"
    _, rows, _ = table(tmp_path, capsys, acked, args=["--ticket", f"S5={H_S5}"])
    assert rows["S5"][0] == "PASS" and "ACCEPTED" in rows["S5"][1]
    _, rows, _ = table(tmp_path, capsys, [ev("place", ticket=H_S5, side="long"), ev("reject", ticket=H_S5, state="REJECTED", code="2")],
                       args=["--ticket", f"S5={H_S5}"])
    assert rows["S5"][0] == "PASS" and "REFUSED it (errorCode 2)" in rows["S5"][1]


def test_s8_order_fields(tmp_path, capsys):
    rows_in = [{"id": 111, "accountId": ACCOUNT, "type": 1, "fillVolume": 1, "customTag": f"px-{H_LONG}-1"},
               {"id": 112, "accountId": ACCOUNT, "type": 4}]
    _, rows, out = table(tmp_path, capsys, orders={"success": True, "orders": rows_in})
    assert rows["S8a"][0] == rows["S8b"][0] == "PASS" and "desk-shaped n=1" in out
    _, rows, _ = table(tmp_path, capsys, orders=[{"id": 113, "type": 1}])            # a bare list is accepted
    assert rows["S8a"][0] == rows["S8b"][0] == "FAIL"
    _, rows, _ = table(tmp_path, capsys, orders={"orders": []})
    assert rows["S8a"][0] == "UNKNOWN"


def test_s9_arm_trail(tmp_path, capsys):
    _, rows, _ = table(tmp_path, capsys, [ev("trail", origin="ui", size=1, state="SENDING"), ev("trail", origin="ui", size=1, state="ARMED")])
    assert rows["S9"][0] == "PASS"
    _, rows, _ = table(tmp_path, capsys, [ev("trail", origin="ui", state="REJECTED")])
    assert rows["S9"][0] == "FAIL" and "rejected the type-5" in rows["S9"][1]
    _, rows, _ = table(tmp_path, capsys, [ev("trail", origin="ui", state="UNSEEN"), ev("halt", code="trail_unverified")])
    assert rows["S9"][0] == "FAIL" and rows["X0"][0] == "FAIL"
    _, rows, _ = table(tmp_path, capsys, [ev("trail", origin="ui", state="CONTEXT_CHANGED")])
    assert rows["S9"][0] == "UNKNOWN"


def test_s10_trail_read_back_fields(tmp_path, capsys):
    good = {"id": 201, "type": 5, "trailPrice": 4.5, "trailDistance": 18, "stopPrice": 6001.25}
    _, rows, _ = table(tmp_path, capsys, orders={"orders": [good, {"id": 202, "type": 4, "stopPrice": 6000.0}]})
    assert rows["S10"][0] == "PASS"
    as_price = dict(good, trailPrice=6005.75)                                    # the level, not the distance: the read-back assumption is wrong
    _, rows, _ = table(tmp_path, capsys, orders={"orders": [as_price]})
    assert rows["S10"][0] == "FAIL"
    _, rows, _ = table(tmp_path, capsys, orders={"orders": [dict(good, stopPrice=None)]})
    assert rows["S10"][0] == "FAIL"
    _, rows, _ = table(tmp_path, capsys, orders={"orders": [dict(good, trailPrice=12.5, trailDistance=25)]}, args=["--tick", "0.5"])
    assert rows["S10"][0] == "PASS"
    _, rows, _ = table(tmp_path, capsys, orders={"orders": [{"id": 203, "type": 4}]})
    assert rows["S10"][0] == "UNKNOWN"


def test_s11_trail_entry_and_s12_one_lot(tmp_path, capsys):
    recs = entry(H_TE, "long") + entry(H_1L, "long")
    recs[len(entry(H_TE, "long"))] = ev("preview", ticket=H_1L, origin="ui", root="MES", side="long", size=1, one_lot=1)
    _, rows, _ = table(tmp_path, capsys, recs)
    assert rows["S11"][0] == rows["S12"][0] == "UNKNOWN"                       # unbound
    _, rows, _ = table(tmp_path, capsys, recs, args=["--ticket", f"S11={H_TE}", "--ticket", f"s12={H_1L}"])
    assert rows["S11"][0] == "PASS" and rows["S12"][0] == "PASS"
    fb = entry(H_TE, "long", extra=[ev("stop", ticket=H_TE, state="TRAIL_FALLBACK_FIXED")])
    _, rows, _ = table(tmp_path, capsys, fb, args=["--ticket", f"S11={H_TE}"])
    assert rows["S11"][0] == "FAIL" and "TRAIL_FALLBACK_FIXED" in rows["S11"][1]
    _, rows, _ = table(tmp_path, capsys, entry(H_1L, "long"), args=["--ticket", f"S12={H_1L}"])
    assert rows["S12"][0] == "FAIL" and "1-lot branch" in rows["S12"][1]


def test_unknown_halt_codes_print_as_other(tmp_path, capsys):
    _, rows, out = table(tmp_path, capsys, [ev("halt", code="deadbeef")])
    assert rows["X0"][0] == "FAIL" and "other n=1" in out and "deadbeef" not in out


# ---- no secrets, ids, hashes or tags in the output --------------------------------------------------------------------------

def test_output_carries_no_ids_hashes_tags_or_balances(tmp_path, capsys):
    recs = entry(H_LONG, "long") + entry(H_S5, "short") + [ev("trail", origin="ui", state="ARMED"), ev("halt", code="manual")]
    for r in recs:
        r.setdefault("risk_usd", 41.25)
    orders = {"orders": [{"id": 777123, "accountId": ACCOUNT, "type": 5, "trailPrice": 4.5, "trailDistance": 18, "stopPrice": 6001.25,
                          "customTag": f"px-{H_LONG}-2", "fillVolume": 1, "balance": 50123.45}]}
    rc, _, out = table(tmp_path, capsys, recs, orders=orders, args=["--ticket", f"S5={H_S5}", "--ticket", f"S11={H_LONG}"])
    assert rc == 0
    for secret in (str(ACCOUNT), "777123", H_LONG, H_S5, "px-", "50123", "41.25", "6001.25", str(tmp_path)):
        assert secret not in out, secret


# ---- standalone ------------------------------------------------------------------------------------------------------------------

def test_the_script_imports_no_desk_module_and_never_uses_a_write_attribute():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
        assert not (isinstance(n, ast.Attribute) and n.attr == "write"), "bare .write( access"
    assert not (mods & {"projectx_desk", "app", "guard", "client", "hub"}), mods
    assert mods <= {"__future__", "argparse", "json", "math", "pathlib", "re", "sys"}, mods     # stdlib only


def test_no_desk_module_imports_or_names_the_script():
    for p in (ROOT / "projectx_desk").rglob("*.py"):
        assert "probe_report" not in p.read_text(encoding="utf-8"), p
