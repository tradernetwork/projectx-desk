#!/usr/bin/env python
"""Score the BIAS rule from data/bias_log.jsonl: did a B or better lean beat a coin flip, window by window?

    python scripts/bias_backtest.py                      # ./data/bias_log.jsonl, rule v2.1, 30 minute horizon, 1.5 ATR stop, 1.5R target
    python scripts/bias_backtest.py --log copy.jsonl --horizon 12 --risk 0.10 --folds 3

Offline: reads one file, prints a report, writes nothing. Read projectx_desk/biasbt.py for the method and its limits (close-only, no costs)."""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk import biasbt  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", default=str(ROOT / "data" / "bias_log.jsonl"))
    ap.add_argument("--rule", default="v2.1")
    ap.add_argument("--horizon", type=int, default=6, help="5m bars to hold (6 = 30 minutes)")
    ap.add_argument("--stop-k", type=float, default=1.5, help="stop distance in ATR")
    ap.add_argument("--rr", type=float, default=1.5, help="target as a multiple of the risk")
    ap.add_argument("--risk", type=float, default=0.10, help="risk_pct of the loss room for the Monte Carlo")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rep = biasbt.report(biasbt.load(a.log), a.rule, a.horizon, a.stop_k, a.rr, a.risk, a.folds, a.seed)
    au = rep["audit"]
    print(f"BIAS BACKTEST  rule={a.rule}  hold={a.horizon} bars  stop={a.stop_k} ATR  target={a.rr}R  (close-only, no costs)")
    print(f"audit: {au['scored_rows']} of {au['rows']} rows scored, rule versions {au['rule_versions']}, repaints={au['repaints']}, gaps={au['gaps']}, no_atr={au['no_atr']}")
    print("\ngrade        n    win   mean R")
    for g, s in rep["by_grade"].items():
        print(f"{g:<10} {s['n']:>4}  {'' if s['win'] is None else format(s['win'], '.0%'):>5}  {'' if s['mean_r'] is None else format(s['mean_r'], '+.3f'):>7}")
    print("\nBY REGIME (all leans; lean R vs the opposite side's R; n overlaps, about n/6 independent)")
    for name, tab in rep["regimes"].items():
        print(f"  {name}")
        for x in tab:
            print(f"    {x['label']:<28} n={x['n']:>4}  lean {x['lean']:+.3f}  opp {x['opp']:+.3f}")
    print(f"\ncoin-flip p (A+B): {rep['coin_flip_p_A_B']}   (chance a random direction does as well; small is good)")
    print("walk-forward folds (A+B):", "  ".join(f"[n={f['n']} win={f['win']:.0%} R={f['mean_r']:+.2f}]" for f in rep["folds_A_B"]) or "too few trades")
    mc = rep["monte_carlo_A_B"]
    if mc:
        print(f"\nmonte carlo, {mc['paths']} paths of {mc['length']} trades at {mc['risk_pct']:.0%} risk: P(3 losses in a row)={mc['p_3_losses_in_a_row']:.0%}, "
              f"P(10 in a row)={mc['p_10_losses_in_a_row']:.2%}, median longest streak={mc['median_longest_losing_streak']}, "
              f"loss room left in the worst 5% of paths={mc['room_left_p05']:.0%}")
    print("\nVERDICT:", rep["verdict"])


if __name__ == "__main__":
    main()
