"""Per-rule, per-chain summary of the forward-test trades (markdown tables).

    python rule_stats.py ../results/forward_trades.csv

Reads the CSV written by export_trades.py, so it needs no raw data and little memory.
Returns are the "half at 2x + trail 35% + SL -35%, 6h" exit; `delay5m` = same exit, entry 5 min later.
"""

import csv
import statistics as st
import sys
from collections import defaultdict

R = "ret_half2x_trail35_sl35_6h"


def fnum(x):
    return float(x) if x != "" else None


def main(path):
    g = defaultdict(list)
    for r in csv.DictReader(open(path)):
        g[(r["rule"], r["chain"])].append(r)
    for chain in ("robinhood", "solana"):
        print(f"\n### {chain}\n")
        print("| rule | trades | total | mean | median | win | +5min delay total | all@2x total | rug ≤6h | total w/o top 5 |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for (rule, ch), v in sorted(g.items()):
            if ch != chain:
                continue
            a = [float(r[R]) for r in v]
            d = [x for x in (fnum(r[R + "_delay5m"]) for r in v) if x is not None]
            b = [float(r["ret_all2x_sl35_6h"]) for r in v]
            rug = sum(r["rug_within_6h"] == "1" for r in v) / len(v)
            top = f"{sum(sorted(a)[:-5]):+.1f}u" if len(a) > 5 else ""
            print(f"| {rule} | {len(v)} | {sum(a):+.1f}u | {st.mean(a):+.0%} | {st.median(a):+.0%} | "
                  f"{sum(x > 0 for x in a) / len(a):.0%} | {sum(d):+.1f}u | {sum(b):+.1f}u | {rug:.0%} | {top} |")
    print("\n### robinhood, total per day (signal day, UTC)\n")
    days = sorted({r["signal_utc"][:10] for v in g.values() for r in v})
    print("| rule | " + " | ".join(d[5:] for d in days) + " |")
    print("|---" * (len(days) + 1) + "|")
    for (rule, ch), v in sorted(g.items()):
        if ch != "robinhood":
            continue
        per = defaultdict(float)
        n = defaultdict(int)
        for r in v:
            per[r["signal_utc"][:10]] += float(r[R])
            n[r["signal_utc"][:10]] += 1
        print(f"| {rule} | " + " | ".join(f"{per[d]:+.1f} ({n[d]})" if n[d] else "" for d in days) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
