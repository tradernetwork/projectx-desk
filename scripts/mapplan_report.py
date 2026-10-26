#!/usr/bin/env python
"""Shadow-mode record of dealer-map proposals: what each trigger would have done, against the opposite side with the same stop. Offline, read-only.

    python scripts/mapplan_report.py [--file data/map_proposals.jsonl]

A rate is withheld under 30 resolved proposals per trigger kind (mapplan.MIN_RESOLVED). Stage 2 (showing a proposal) needs a kind to beat its
baseline over a real sample first; see docs/DECISIONS.md, Round 21."""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk import mapplan as MP  # noqa: E402
from projectx_desk import mapplan_log as L  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", default=str(ROOT / "data" / "map_proposals.jsonl"))
    a = ap.parse_args()
    rows = L.load(a.file)
    props = [r for r in rows if r["kind_row"] == "proposal"]
    outs = [r for r in rows if r["kind_row"] == "outcome"]
    print(f"{len(props)} proposals filed, {len(outs)} graded ({len(props) - len(outs)} pending)")
    print(f"\n{'trigger':<18}{'filed':>6}{'graded':>8}{'mean R':>9}{'opp R':>8}{'stop%':>7}{'tgt%':>6}")
    for s in MP.stats(props, outs):
        f = lambda v, nd=3: "withheld" if v is None else f"{v:.{nd}f}"
        print(f"{s['kind']:<18}{s['proposed']:>6}{s['resolved']:>8}{f(s['mean_r']):>9}{f(s['mean_r_opp']):>8}{f(s['stop_share'], 2):>7}{f(s['target_share'], 2):>6}")
    print(f"\n'withheld' = under {MP.MIN_RESOLVED} graded. Close-only bars, a bar touching stop and target counts as the stop, no costs. Shadow only: nothing is shown or stageable.")


if __name__ == "__main__":
    main()
