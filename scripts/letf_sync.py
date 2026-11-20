#!/usr/bin/env python3
"""Build projectx_desk/rh/letf_data.json, the vendored leveraged-ETF universe for the desk's LETF view.

    python scripts/letf_sync.py                       # download the three CSVs, validate, rewrite the JSON
    python scripts/letf_sync.py --from-dir DIR        # read the CSVs from a folder instead (no network)
    python scripts/letf_sync.py --check               # exit 1 when the committed JSON differs from what these inputs would build
    python scripts/letf_sync.py --strict              # exit 1 when a label is neither a ticker nor in letf_proxies.py

Source: https://github.com/mphinance/leveraged-etfs (data/*.csv). The CSVs are untrusted text: they are held in memory (never
written to disk), size-capped, redirects are refused, every field is checked against a strict pattern, and ANY failure rejects the
whole run and leaves the old file in place. The desk itself never makes this request; it only reads the committed JSON.

Issuer: the inverse CSV has an `issuer` column; for longs it is derived from the start of `fund_name` against LONG_ISSUERS (else null).
The source has no expense ratio, AUM, inception date or yield, so none is shown.

Dropped on purpose: `last` and `avg_shares_20d` (reverse splits make them misleading; live quotes replace them) and any fund with
leverage inside +/-1.5. Shorts join an underlying on the CSV `bucket` column (the inverse `underlying` column is messy).
Never uses a bare `.write(`: the guard pin rejects it everywhere outside the order guards. Files are written with json.dump and
os.replace.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from projectx_desk.rh.letf_proxies import BUCKET_ALIASES, NO_PROXY, PROXIES  # noqa: E402
from projectx_desk.rh.views import SYMBOL  # noqa: E402

BASE = "https://raw.githubusercontent.com/mphinance/leveraged-etfs/main/data/"
SOURCE = "mphinance/leveraged-etfs data/"
OUT = ROOT / "projectx_desk" / "rh" / "letf_data.json"
MAX_BYTES = 2 * 1024 * 1024
MIN_ROWS, MAX_ROWS = 50, 5000

LONG_COLS = ["symbol", "leverage", "bucket", "group", "fund_name", "avg_shares_20d", "avg_dollar_vol_20d", "last"]
INV_COLS = ["symbol", "leverage", "underlying", "group", "issuer", "avg_shares_20d", "avg_dollar_vol_20d", "last", "bucket"]
BEST_COLS = ["underlying", "group", "long_pick", "long_leverage", "long_avg_dollar_vol_20d", "long_other_funds",
             "short_pick", "short_leverage", "short_avg_dollar_vol_20d", "short_other_funds"]

GROUPS = [("index", "Broad index"), ("sector", "Sector"), ("single-stock", "Single stock"), ("commodity", "Commodity"),
          ("crypto", "Crypto"), ("bond", "Bonds"), ("country", "Country / region"), ("currency", "Currency"),
          ("volatility", "Volatility")]
GROUP_SLUGS = frozenset(g for g, _ in GROUPS)
NO_SELF_CHART = frozenset({"crypto", "commodity", "currency", "volatility"})   # a label here never charts itself

LONG_LEV = (1.5, 5.0)
SHORT_LEV = (-5.0, -1.5)
RE_BUCKET = re.compile(r"[A-Za-z0-9 &.,'()/+\-]{1,48}")
RE_NAME = re.compile(r"[A-Za-z0-9 &.,'()/+%\-]{1,96}")
RE_ISSUER = re.compile(r"[A-Za-z0-9 &.\-]{1,40}")
# Long-fund issuers: the long CSV has no issuer column, so it is read from the start of fund_name, and ONLY for these names
# (lowercase prefix -> the name shown). Built from the fund names actually in the data; an unknown prefix is None, never a guess.
# Matched on a whole leading word group (the prefix must be followed by a space or the end of the name).
LONG_ISSUERS = {
    "leverage shares": "Leverage Shares", "direxion": "Direxion", "tradr": "Tradr", "proshares": "ProShares",
    "defiance": "Defiance", "graniteshares": "GraniteShares", "t-rex": "T-REX", "corgi": "Corgi",
    "microsectors": "MicroSectors", "teucrium": "Teucrium", "21shares": "21Shares", "roundhill": "Roundhill",
    "advisorshares": "AdvisorShares", "uscf": "USCF",
}


def issuer_from_name(name):
    """Issuer for a long fund from its cleaned fund_name, or None. Case-insensitive, whole leading words only."""
    low = (name or "").lower()
    for pre, shown in LONG_ISSUERS.items():
        if low == pre or low.startswith(pre + " "):
            return shown
    return None


class SyncError(Exception):
    """Any input problem. The message names the file and row; nothing is written."""


def _rows(name, text, cols):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise SyncError(f"{name}: over {MAX_BYTES} bytes")
    rd = csv.reader(io.StringIO(text))
    try:
        head = next(rd)
    except StopIteration:
        raise SyncError(f"{name}: empty") from None
    if head != cols:
        raise SyncError(f"{name}: header is {head}, expected {cols}")
    out = []
    for i, row in enumerate(rd, start=2):
        if len(row) != len(cols):
            raise SyncError(f"{name} line {i}: {len(row)} fields, expected {len(cols)}")
        out.append(dict(zip(cols, row)))
    if not MIN_ROWS <= len(out) <= MAX_ROWS:
        raise SyncError(f"{name}: {len(out)} rows, expected {MIN_ROWS} to {MAX_ROWS}")
    return out


def _num(name, line, field, raw, lo=None):
    """Blank is None (unknown, never 0); anything else must be a finite number."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        raise SyncError(f"{name} {line}: {field} is not a number: {raw[:20]!r}") from None
    if not math.isfinite(v) or (lo is not None and v < lo):
        raise SyncError(f"{name} {line}: {field} out of range: {raw[:20]!r}")
    return v


def _clean_name(raw, warnings, sym):
    s = re.sub(r"\s+", " ", (raw or "").replace("\u00d7", "x").replace("\u2122", "")).strip()
    if not s:
        return None
    if not RE_NAME.fullmatch(s):
        warnings.append(f"fund_name dropped for {sym}: unexpected characters")
        return None
    return s


def _funds(longs, inv, warnings):
    funds, seen, dropped = {}, set(), 0
    for side, rows, name in (("long", longs, "all_long_etfs.csv"), ("short", inv, "all_inverse_etfs.csv")):
        for n, r in enumerate(rows, start=2):
            sym = r["symbol"].strip()
            if not SYMBOL.fullmatch(sym):
                raise SyncError(f"{name} line {n}: bad symbol {sym[:12]!r}")
            if sym in seen:
                raise SyncError(f"{name} line {n}: duplicate symbol {sym}")
            seen.add(sym)
            lev = _num(name, n, "leverage", r["leverage"])
            if lev is None:
                raise SyncError(f"{name} line {n}: leverage is blank")
            lo, hi = LONG_LEV if side == "long" else SHORT_LEV
            if not lo <= lev <= hi:
                dropped += 1
                continue
            if r["group"] not in GROUP_SLUGS:
                raise SyncError(f"{name} line {n}: unknown group {r['group'][:20]!r}")
            bucket = r["bucket"].strip()
            if side == "short":
                bucket = BUCKET_ALIASES.get(bucket, bucket)
            if not RE_BUCKET.fullmatch(bucket):
                raise SyncError(f"{name} line {n}: bad bucket {bucket[:30]!r}")
            dv = _num(name, n, "avg_dollar_vol_20d", r["avg_dollar_vol_20d"], lo=0)
            issuer = None
            fname = None
            if side == "long":
                fname = _clean_name(r["fund_name"], warnings, sym)
                issuer = issuer_from_name(fname)
            else:
                iss = r["issuer"].strip()
                if iss and RE_ISSUER.fullmatch(iss):
                    issuer = iss
                elif iss:
                    warnings.append(f"issuer dropped for {sym}: unexpected characters")
            funds[sym] = {"u": bucket, "lev": lev, "dv20": dv, "name": fname, "issuer": issuer, "_group": r["group"]}
    if dropped:
        warnings.append(f"{dropped} funds outside the +/-1.5x to 5x range left out")
    return funds


def _rank(syms, funds):
    return sorted(syms, key=lambda s: (funds[s]["dv20"] is None, -(funds[s]["dv20"] or 0.0), s))


def _classify(key, group):
    """-> (chart_kind, chart symbol or None, quality or None, unmapped?)"""
    if key in PROXIES:
        sym, q = PROXIES[key]
        if not SYMBOL.fullmatch(sym) or q not in ("exact", "approximate"):
            raise SyncError(f"letf_proxies.py: bad entry for {key!r}")
        return "proxy", sym, q, False
    if key in NO_PROXY:
        return "none", None, None, False
    if group in NO_SELF_CHART:
        return "none", None, None, False
    if SYMBOL.fullmatch(key):
        return "self", key, None, False
    return "none", None, None, True


def build(longs_csv, inv_csv, best_csv, as_of):
    """Three CSV texts in, (document, warnings, unmapped labels) out. Raises SyncError on any bad input."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of or ""):
        raise SyncError("as_of must be YYYY-MM-DD")
    warnings = []
    longs = _rows("all_long_etfs.csv", longs_csv, LONG_COLS)
    inv = _rows("all_inverse_etfs.csv", inv_csv, INV_COLS)
    best = _rows("best_per_underlying.csv", best_csv, BEST_COLS)
    funds = _funds(longs, inv, warnings)
    by_u = {}
    for sym, f in funds.items():
        by_u.setdefault(f["u"], {"long": [], "short": []})["long" if f["lev"] > 0 else "short"].append(sym)

    unders, unmapped, seen_keys = [], [], set()
    for n, b in enumerate(best, start=2):
        key = b["underlying"].strip()
        if not RE_BUCKET.fullmatch(key):
            raise SyncError(f"best_per_underlying.csv line {n}: bad underlying {key[:30]!r}")
        if key in seen_keys:
            raise SyncError(f"best_per_underlying.csv line {n}: duplicate underlying {key}")
        seen_keys.add(key)
        group = b["group"]
        if group not in GROUP_SLUGS:
            raise SyncError(f"best_per_underlying.csv line {n}: unknown group {group[:20]!r}")
        sides = by_u.get(key, {"long": [], "short": []})
        if not sides["long"] and not sides["short"]:
            warnings.append(f"{key}: no fund left after the leverage filter, skipped")
            continue
        picks = {}
        for side, col in (("long", "long_pick"), ("short", "short_pick")):
            ranked = _rank(sides[side], funds)
            want = b[col].strip()
            if want and want in sides[side]:
                picks[side] = want
            else:
                picks[side] = ranked[0] if ranked else None
                if want and ranked:
                    warnings.append(f"{key}: {side} pick {want} not in the filtered set, using {picks[side]}")
        kind, chart, quality, un = _classify(key, group)
        if un:
            unmapped.append(key)
        unders.append({
            "key": key, "group": group, "chart": chart, "chart_kind": kind, "proxy_quality": quality,
            "long_pick": picks["long"], "short_pick": picks["short"],
            "long": _rank(sides["long"], funds), "short": _rank(sides["short"], funds),
        })
    placed = {s for u in unders for s in u["long"] + u["short"]}
    for sym in sorted(set(funds) - placed):
        warnings.append(f"{sym}: bucket {funds[sym]['u']!r} matches no underlying, left out")
    out_funds = {s: {k: v for k, v in funds[s].items() if not k.startswith("_")} for s in sorted(placed)}
    doc = {"version": 2, "as_of": as_of, "source": SOURCE, "groups": [list(g) for g in GROUPS],
           "underlyings": unders, "funds": out_funds}
    return doc, warnings, sorted(unmapped)


def _fetch(name):
    import httpx
    r = httpx.get(BASE + name, follow_redirects=False, timeout=30.0)
    if r.status_code != 200:
        raise SyncError(f"{name}: HTTP {r.status_code}")
    if len(r.content) > MAX_BYTES:
        raise SyncError(f"{name}: over {MAX_BYTES} bytes")
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError:
        raise SyncError(f"{name}: not UTF-8") from None


def _read_dir(d, name):
    p = Path(d) / name
    if p.stat().st_size > MAX_BYTES:
        raise SyncError(f"{name}: over {MAX_BYTES} bytes")
    try:
        return p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SyncError(f"{name}: not UTF-8") from None


def dump_text(doc):
    return json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def save(doc, path):
    """Atomic: a temp file in the same folder, then os.replace. A crash leaves the old file or the new one, never half."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            print(dump_text(doc), file=f, end="")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _summary(old, new):
    if not old:
        return "new file"
    of, nf = set(old.get("funds", {})), set(new["funds"])
    op = {u["key"]: (u["long_pick"], u["short_pick"]) for u in old.get("underlyings", [])}
    np_ = {u["key"]: (u["long_pick"], u["short_pick"]) for u in new["underlyings"]}
    changed = sorted(k for k in np_ if k in op and op[k] != np_[k])
    return (f"funds +{len(nf - of)} -{len(of - nf)}; underlyings +{len(set(np_) - set(op))} -{len(set(op) - set(np_))}; "
            f"picks changed {len(changed)}" + (": " + ", ".join(changed[:10]) if changed else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from-dir", help="read the three CSVs from this folder instead of downloading")
    ap.add_argument("--as-of", help="data date, YYYY-MM-DD (default: the date already in the committed file)")
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed JSON would change")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a label is neither a ticker nor in letf_proxies.py")
    ap.add_argument("--out", default=str(OUT), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    out = Path(a.out)
    old = None
    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
    as_of = a.as_of or (old or {}).get("as_of")
    if not as_of:
        print("letf_sync: --as-of YYYY-MM-DD is required when there is no committed file yet", file=sys.stderr)
        return 2
    names = ("all_long_etfs.csv", "all_inverse_etfs.csv", "best_per_underlying.csv")
    try:
        texts = [_read_dir(a.from_dir, n) if a.from_dir else _fetch(n) for n in names]
        doc, warnings, unmapped = build(*texts, as_of)
    except (SyncError, OSError) as e:
        print(f"letf_sync: rejected, nothing written: {e}", file=sys.stderr)
        return 1
    for w in warnings:
        print("warning:", w)
    for u in unmapped:
        print(f"UNMAPPED label={u}")
    print(f"{len(doc['underlyings'])} underlyings, {len(doc['funds'])} funds, as of {doc['as_of']}")
    if a.strict and unmapped:
        return 1
    if a.check:
        same = out.exists() and out.read_text(encoding="utf-8") == dump_text(doc)
        print("committed file is current" if same else "committed file differs: run scripts/letf_sync.py")
        return 0 if same else 1
    print(_summary(old, doc))
    save(doc, out)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
