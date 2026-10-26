#!/usr/bin/env python
"""Paper trading journal: record hypothetical trades from the desk's B-or-better grades and the analyst ENTRY callouts, and grade them.

No account, no orders, no desk write: it reads the desk's own loopback API (bars, callouts) and data/bias_log.jsonl, and appends to
data/paper_trades.jsonl. Run it every few minutes on the machine the desk runs on (see the cron line below), or by hand:

    python scripts/paper_journal.py                 # open fresh signals, grade open trades, print the summary
    python scripts/paper_journal.py --report        # just print the summary
    python scripts/paper_journal.py --desk http://127.0.0.1:8600 --data data

    */5 * * * * cd ~/projects/projectx-desk && .venv/bin/python scripts/paper_journal.py >> data/paper_journal.log 2>&1

Rules live in projectx_desk/paper.py (pure, tested): entry at the closed bar's close, 1.5 ATR stop, 1.5R target, a bar touching both counts as the
stop, flat at 15:08 CT, one open trade per contract, the opposite side as the baseline, no rate under 30 trades.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk import paper, sizing  # noqa: E402


def read_jsonl(path, tail=20000):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def get(desk, path, timeout=15):
    with urllib.request.urlopen(desk.rstrip("/") + path, timeout=timeout) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--desk", default="http://127.0.0.1:8600")
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--report", action="store_true", help="print the summary and exit")
    a = ap.parse_args()
    data = pathlib.Path(a.data)
    jpath = data / "paper_trades.jsonl"
    journal = read_jsonl(jpath)
    if a.report:
        print(paper.report(journal))
        return 0
    now_ms = int(time.time() * 1000)
    bias = read_jsonl(data / "bias_log.jsonl", tail=4000)
    cons = {}
    for r in bias:                                         # root -> the contract the desk logs it under
        c = r.get("contract")
        if isinstance(c, str) and c.count(".") >= 3:
            cons[c.split(".")[3]] = c
    try:
        callouts = get(a.desk, "/api/callouts").get("items", [])
    except Exception as e:  # noqa: BLE001 - the callout feed is optional
        callouts = []
        print("callouts unavailable:", type(e).__name__)

    def tick_for(cid):
        try:
            return sizing.spec_for(cid).tick_size
        except Exception:  # noqa: BLE001
            return None
    new = paper.open_new(journal, bias, callouts, cons.get, tick_for, now_ms)
    journal += new

    def bars_for(cid):
        try:
            rows = get(a.desk, "/bars?" + urllib.parse.urlencode({"contract": cid, "unit": 2, "n": 5, "limit": 400}))
            return rows if isinstance(rows, list) else rows.get("bars")
        except Exception as e:  # noqa: BLE001
            print("bars unavailable for", cid.split(".")[3], type(e).__name__)
            return None
    closes = paper.close_due(journal, bars_for, now_ms)
    journal += closes
    if new or closes:
        jpath.parent.mkdir(parents=True, exist_ok=True)
        with open(jpath, "a", encoding="utf-8") as f:
            for r in new + closes:
                print(json.dumps(r), file=f)
    print(f"opened {len(new)}, closed {len(closes)}")
    print(paper.report(journal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
