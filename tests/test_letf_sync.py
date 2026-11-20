"""scripts/letf_sync.py and the committed projectx_desk/rh/letf_data.json (the LETF desk's vendored universe)."""
import csv
import importlib.util
import io
import json
import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("letf_sync", ROOT / "scripts" / "letf_sync.py")
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)

from projectx_desk.rh import letf_proxies  # noqa: E402
from projectx_desk.rh.views import SYMBOL  # noqa: E402


def _csv(cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return buf.getvalue()


def _inputs(extra_long=(), extra_inv=(), extra_best=(), pad=60):
    """A small valid universe: `pad` filler single stocks (so the 50-row floor holds) plus whatever a test adds."""
    longs, inv, best = [], [], []
    for i in range(pad):
        u = f"S{i:03d}"
        longs.append({"symbol": f"L{i:03d}", "leverage": "2.0", "bucket": u, "group": "single-stock",
                      "fund_name": f"Acme 2X Long {u}", "avg_dollar_vol_20d": str(1e6 + i)})
        inv.append({"symbol": f"I{i:03d}", "leverage": "-2.0", "underlying": u, "group": "single-stock",
                    "issuer": "Acme", "avg_dollar_vol_20d": str(2e6 + i), "bucket": u})
        best.append({"underlying": u, "group": "single-stock", "long_pick": f"L{i:03d}", "short_pick": f"I{i:03d}"})
    longs += list(extra_long)
    inv += list(extra_inv)
    best += list(extra_best)
    return (_csv(ls.LONG_COLS, longs), _csv(ls.INV_COLS, inv), _csv(ls.BEST_COLS, best))


def _long(sym, lev, bucket, group="single-stock", name="Acme 2X Long", dv="5000000"):
    return {"symbol": sym, "leverage": str(lev), "bucket": bucket, "group": group, "fund_name": name, "avg_dollar_vol_20d": dv}


def _inv(sym, lev, bucket, group="single-stock", issuer="Acme", dv="4000000"):
    return {"symbol": sym, "leverage": str(lev), "underlying": bucket, "group": group, "issuer": issuer,
            "avg_dollar_vol_20d": dv, "bucket": bucket}


def _best(u, group, lp="", sp=""):
    return {"underlying": u, "group": group, "long_pick": lp, "short_pick": sp}


def _u(doc, key):
    return next(x for x in doc["underlyings"] if x["key"] == key)


def test_filters_leverage_and_joins_shorts_on_bucket():
    texts = _inputs(
        extra_long=[_long("TLO", 3.0, "Gold", "commodity"), _long("ONEQ", 1.25, "Gold", "commodity")],
        extra_inv=[_inv("GLS", -2.0, "Gold", "commodity"), _inv("GL1", -1.0, "Gold", "commodity"),
                   _inv("ARKS", -2.0, "ARKK", "sector")],
        extra_best=[_best("Gold", "commodity", "TLO", "GLS"), _best("ARKK / Innovation", "sector", "", "")],
    )
    doc, warns, _ = ls.build(*texts, "2026-11-19")
    g = _u(doc, "Gold")
    assert g["long"] == ["TLO"] and g["short"] == ["GLS"]
    assert "ONEQ" not in doc["funds"] and "GL1" not in doc["funds"]        # 1.25x and -1x are outside +/-1.5x
    assert doc["funds"]["TLO"]["lev"] == 3.0 and doc["funds"]["GLS"]["lev"] == -2.0
    assert _u(doc, "ARKK / Innovation")["short"] == ["ARKS"]               # bucket alias ARKK -> ARKK / Innovation
    assert any("outside" in w for w in warns)


@pytest.mark.parametrize("mutate", [
    lambda t: (t[0].replace("symbol,leverage", "sym,leverage", 1), t[1], t[2]),                 # header
    lambda t: (t[0].replace("L001,", "L000,", 1), t[1], t[2]),                                  # duplicate symbol
    lambda t: (t[0].replace("single-stock", "weird", 1), t[1], t[2]),                           # unknown group
    lambda t: (t[0].replace("L002,2.0", "L002,nan", 1), t[1], t[2]),                            # non-finite
    lambda t: (t[0].replace("L003,2.0", "l003;DROP,2.0", 1), t[1], t[2]),                       # bad symbol
    lambda t: (t[0], t[1], t[2].replace("S004,single-stock", "S004,single-stock,extra", 1)),    # field count
    lambda t: (t[0], t[1], t[2].replace("S005", "S004", 1)),                                    # duplicate underlying
])
def test_rejects_bad_header_duplicate_symbol_bad_group_and_more(mutate):
    with pytest.raises(ls.SyncError):
        ls.build(*mutate(_inputs()), "2026-11-19")


def test_rejects_too_few_rows_and_oversize():
    with pytest.raises(ls.SyncError):
        ls.build(*_inputs(pad=5), "2026-11-19")
    big = _inputs()
    with pytest.raises(ls.SyncError):
        ls.build(big[0] + "#" * (ls.MAX_BYTES + 1), big[1], big[2], "2026-11-19")
    with pytest.raises(ls.SyncError):
        ls.build(*_inputs(), "20261119")


def test_hostile_fund_name_dropped_not_fatal_and_symbols_normalised():
    texts = _inputs(extra_long=[_long("EVIL", 2.0, "S000", name="<script>alert(1)</script>"),
                                _long("TM", 2.0, "S001", name="Acme\u2122 2\u00d7 Long   Fund")],
                    extra_best=[])
    doc, warns, _ = ls.build(*texts, "2026-11-19")
    assert doc["funds"]["EVIL"]["name"] is None
    assert any("fund_name dropped for EVIL" in w for w in warns)
    assert doc["funds"]["TM"]["name"] == "Acme 2x Long Fund"                # tm stripped, x normalised, spaces collapsed
    for f in doc["funds"].values():
        assert f["name"] is None or re.fullmatch(r"[A-Za-z0-9 &.,'()/+%\-]{1,96}", f["name"])


def test_blank_dollar_volume_is_null_never_zero_and_sorts_last():
    texts = _inputs(extra_long=[_long("NOVOL", 2.0, "S002", dv="")])
    doc, _, _ = ls.build(*texts, "2026-11-19")
    assert doc["funds"]["NOVOL"]["dv20"] is None
    assert _u(doc, "S002")["long"][-1] == "NOVOL"                           # unknown volume ranks after known volume
    with pytest.raises(ls.SyncError):
        ls.build(*_inputs(extra_long=[_long("NEG", 2.0, "S002", dv="-1")]), "2026-11-19")


def test_pick_missing_falls_back_to_top_volume():
    texts = _inputs(extra_long=[_long("AAA1", 2.0, "Gold", "commodity", dv="1000000"),
                                _long("BBB1", 2.0, "Gold", "commodity", dv="9000000")],
                    extra_best=[_best("Gold", "commodity", "GONE", "")])
    doc, warns, _ = ls.build(*texts, "2026-11-19")
    assert _u(doc, "Gold")["long_pick"] == "BBB1"
    assert _u(doc, "Gold")["short_pick"] is None
    assert any("GONE" in w for w in warns)


def test_crypto_and_commodity_labels_never_self_chart():
    texts = _inputs(
        extra_long=[_long("XL1", 2.0, "HYPE", "crypto"), _long("XL2", 2.0, "BTC", "crypto"),
                    _long("XL3", 2.0, "QQQ", "index"), _long("XL4", 2.0, "ZZZZ", "commodity"),
                    _long("XL5", 2.0, "Gold", "commodity"), _long("XL6", 2.0, "Mystery Label", "sector")],
        extra_best=[_best("HYPE", "crypto", "XL1"), _best("BTC", "crypto", "XL2"), _best("QQQ", "index", "XL3"),
                    _best("ZZZZ", "commodity", "XL4"), _best("Gold", "commodity", "XL5"),
                    _best("Mystery Label", "sector", "XL6")])
    doc, _, unmapped = ls.build(*texts, "2026-11-19")
    assert _u(doc, "HYPE")["chart_kind"] == "none" and _u(doc, "HYPE")["chart"] is None       # a ticker-shaped crypto label
    assert _u(doc, "BTC")["chart"] == "IBIT" and _u(doc, "BTC")["chart_kind"] == "proxy"       # only through the proxy map
    assert _u(doc, "ZZZZ")["chart_kind"] == "none"                                              # commodity: never self
    assert _u(doc, "QQQ")["chart_kind"] == "self" and _u(doc, "QQQ")["chart"] == "QQQ"
    assert _u(doc, "Gold")["chart"] == "GLD" and _u(doc, "Gold")["proxy_quality"] == "exact"
    assert _u(doc, "Mystery Label")["chart_kind"] == "none"
    assert unmapped == ["Mystery Label"]                                                       # new non-ticker label is reported


def _write_dir(tmp_path, texts):
    for name, t in zip(("all_long_etfs.csv", "all_inverse_etfs.csv", "best_per_underlying.csv"), texts):
        (tmp_path / name).write_text(t, encoding="utf-8")


def test_unmapped_label_reported_and_strict_exits_1(tmp_path, capsys):
    texts = _inputs(extra_long=[_long("XL6", 2.0, "Mystery Label", "sector")],
                    extra_best=[_best("Mystery Label", "sector", "XL6")])
    d = tmp_path / "in"
    d.mkdir()
    _write_dir(d, texts)
    out = tmp_path / "out.json"
    assert ls.main(["--from-dir", str(d), "--as-of", "2026-11-19", "--out", str(out), "--strict"]) == 1
    assert "UNMAPPED label=Mystery Label" in capsys.readouterr().out
    assert not out.exists()                                                  # strict exits before writing
    assert ls.main(["--from-dir", str(d), "--as-of", "2026-11-19", "--out", str(out)]) == 0
    assert out.exists()


def test_check_flags_a_stale_committed_file(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    _write_dir(d, _inputs())
    out = tmp_path / "out.json"
    assert ls.main(["--from-dir", str(d), "--as-of", "2026-11-19", "--out", str(out)]) == 0
    assert ls.main(["--from-dir", str(d), "--out", str(out), "--check"]) == 0
    out.write_text(out.read_text().replace("2026-11-19", "2026-11-18"), encoding="utf-8")
    assert ls.main(["--from-dir", str(d), "--as-of", "2026-11-19", "--out", str(out), "--check"]) == 1


def test_a_rejected_run_keeps_the_old_file(tmp_path, capsys):
    d = tmp_path / "in"
    d.mkdir()
    _write_dir(d, _inputs())
    out = tmp_path / "out.json"
    assert ls.main(["--from-dir", str(d), "--as-of", "2026-11-19", "--out", str(out)]) == 0
    before = out.read_text()
    (d / "all_long_etfs.csv").write_text("garbage\n", encoding="utf-8")
    assert ls.main(["--from-dir", str(d), "--out", str(out)]) == 1
    assert out.read_text() == before
    assert "rejected, nothing written" in capsys.readouterr().err


def test_atomic_write_no_partial_file(tmp_path, monkeypatch):
    out = tmp_path / "out.json"
    ls.save({"a": 1}, out)
    good = out.read_text()

    def boom(*_a, **_k):
        raise OSError("disk")
    monkeypatch.setattr(ls.os, "replace", boom)
    with pytest.raises(OSError):
        ls.save({"a": 2}, out)
    assert out.read_text() == good                                          # old file intact
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("out.json")] == ["out.json"]   # temp file cleaned up
    assert (out.stat().st_mode & 0o777) == 0o644


def test_script_makes_no_network_call_with_from_dir(tmp_path, monkeypatch):
    import httpx

    def no_net(*_a, **_k):
        raise AssertionError("network call")
    monkeypatch.setattr(httpx, "get", no_net)
    d = tmp_path / "in"
    d.mkdir()
    _write_dir(d, _inputs())
    assert ls.main(["--from-dir", str(d), "--as-of", "2026-11-19", "--out", str(tmp_path / "o.json")]) == 0


def test_fetch_refuses_redirects_and_non_200(monkeypatch):
    import httpx
    seen = {}

    class R:
        status_code = 302
        content = b""

    def fake_get(url, **kw):
        seen.update(kw)
        return R()
    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(ls.SyncError):
        ls._fetch("all_long_etfs.csv")
    assert seen["follow_redirects"] is False


# ---- the committed file --------------------------------------------------------------------------------------------------------

DATA = ROOT / "projectx_desk" / "rh" / "letf_data.json"


@pytest.fixture(scope="module")
def committed():
    return json.loads(DATA.read_text(encoding="utf-8"))


def test_committed_json_matches_proxies_and_validates(committed):
    d = committed
    assert d["version"] == 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d["as_of"])
    assert [g for g, _ in d["groups"]] == [g for g, _ in ls.GROUPS]
    funds = d["funds"]
    seen = set()
    for u in d["underlyings"]:
        key = u["key"]
        assert key not in seen
        seen.add(key)
        assert u["group"] in ls.GROUP_SLUGS
        if key in letf_proxies.PROXIES:
            sym, q = letf_proxies.PROXIES[key]
            assert (u["chart_kind"], u["chart"], u["proxy_quality"]) == ("proxy", sym, q)
        elif key in letf_proxies.NO_PROXY or u["group"] in ls.NO_SELF_CHART:
            assert (u["chart_kind"], u["chart"]) == ("none", None)
        elif SYMBOL.fullmatch(key):
            assert (u["chart_kind"], u["chart"]) == ("self", key)
        else:
            assert u["chart_kind"] == "none"
        if u["chart"] is not None:
            assert SYMBOL.fullmatch(u["chart"])
        for side, sign in (("long", 1), ("short", -1)):
            assert u[side + "_pick"] is None or u[side + "_pick"] in u[side]
            for s in u[side]:
                assert funds[s]["u"] == key and funds[s]["lev"] * sign >= 1.5
    for sym, f in funds.items():
        assert SYMBOL.fullmatch(sym)
        assert f["dv20"] is None or (math.isfinite(f["dv20"]) and f["dv20"] >= 0)
        assert 1.5 <= abs(f["lev"]) <= 5
    assert len(funds) == 694 and len(d["underlyings"]) == 315                  # 574 long + 120 short, as of 2026-11-19
    assert sum(1 for f in funds.values() if f["lev"] > 0) == 574
    assert sum(1 for f in funds.values() if f["lev"] < 0) == 120


def test_every_proxy_entry_is_valid_and_used(committed):
    keys = {u["key"] for u in committed["underlyings"]}
    for label, (sym, q) in letf_proxies.PROXIES.items():
        assert SYMBOL.fullmatch(sym) and q in ("exact", "approximate")
        assert label in keys, f"proxy for a label that is not in the data: {label}"
    assert not (set(letf_proxies.PROXIES) & letf_proxies.NO_PROXY)


def test_known_rows(committed):
    f = committed["funds"]
    assert f["TQQQ"]["lev"] == 3.0 and f["SQQQ"]["lev"] == -3.0
    q = _u(committed, "QQQ")
    assert (q["long_pick"], q["short_pick"], q["chart"]) == ("TQQQ", "SQQQ", "QQQ")


def test_no_dashes_in_new_files():
    em, en = chr(0x2014), chr(0x2013)
    for p in (ROOT / "scripts" / "letf_sync.py", ROOT / "projectx_desk" / "rh" / "letf_proxies.py", Path(__file__)):
        text = p.read_text(encoding="utf-8")
        assert em not in text and en not in text, p.name
