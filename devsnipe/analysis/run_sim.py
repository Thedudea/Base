"""Parameter sweep of GMGN Dev Snipe settings over the real trade tapes.

    python run_sim.py ../data/v1/<dev> [draws]
Writes <dev>/sim_results.json and prints the best configurations.
"""

import itertools
import json
import os
import sys
import time

from parse import load_dev
from simulate import LATENCY_PRESETS, Costs, Strategy, fixed_latency, run_many, summarize

TPS = [5, 10, 15, 20, 30, 50]
SLS = [-15, -25, -35, -50]
AMOUNTS = [0.05, 0.1, 0.25, 0.5, 1.0]


def sweep(tokens, dev, draws):
    costs = Costs()
    results = []
    t0 = time.time()
    for lat_name, lat in LATENCY_PRESETS.items():
        for tp, sl, dev_exit in itertools.product(TPS, SLS, [False, True]):
            strat = Strategy(amount_sol=0.1, levels=[(tp, 100), (sl, 100)], exit_on_dev_sell=dev_exit)
            rows = run_many(tokens, strat, lat, costs, n_draws=draws, dev=dev)
            s = summarize(rows, strat.amount_sol)
            s.update(latency=lat_name, tp=tp, sl=sl, dev_exit=dev_exit, amount=0.1)
            results.append(s)
    print(f"grid done in {time.time()-t0:.0f}s")
    return results


def amount_sweep(tokens, dev, draws, best):
    costs = Costs()
    out = []
    for amt in AMOUNTS:
        for lat_name, lat in LATENCY_PRESETS.items():
            strat = Strategy(amount_sol=amt, levels=[(best["tp"], 100), (best["sl"], 100)], exit_on_dev_sell=best["dev_exit"])
            s = summarize(run_many(tokens, strat, lat, costs, n_draws=draws, dev=dev), amt)
            s.update(latency=lat_name, amount=amt, tp=best["tp"], sl=best["sl"], dev_exit=best["dev_exit"])
            out.append(s)
    return out


def latency_matrix(tokens, dev, draws, best):
    costs = Costs()
    out = []
    for L in [0, 1, 2, 3, 4, 6]:
        for R in [1, 2, 3, 5, 8]:
            strat = Strategy(amount_sol=0.1, levels=[(best["tp"], 100), (best["sl"], 100)], exit_on_dev_sell=best["dev_exit"])
            s = summarize(run_many(tokens, strat, fixed_latency(L, R), costs, n_draws=draws, dev=dev), 0.1)
            s.update(L=L, R=R)
            out.append(s)
    return out


def per_token(tokens, dev, draws, best):
    costs = Costs()
    out = []
    lat = LATENCY_PRESETS["typical"]
    strat = Strategy(amount_sol=0.1, levels=[(best["tp"], 100), (best["sl"], 100)], exit_on_dev_sell=best["dev_exit"])
    for tok in tokens:
        s = summarize(run_many([tok], strat, lat, costs, n_draws=draws, dev=dev), 0.1)
        s["mint"] = tok["mint"]
        out.append(s)
    return out


def main():
    d = sys.argv[1]
    draws = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    dev = os.path.basename(os.path.normpath(d))
    tokens = [t for t in load_dev(d) if t["trades"]]
    print(f"{dev}: {len(tokens)} tokens with trades")
    grid = sweep(tokens, dev, draws)
    typical = [g for g in grid if g["latency"] == "typical"]
    typical.sort(key=lambda g: -g["mean_pnl_pct"])
    best = typical[0]
    print("\nTop configs under 'typical' latency (amount 0.1 SOL):")
    for g in typical[:10]:
        print(f"  TP {g['tp']:>3}% SL {g['sl']:>4}% devExit {g['dev_exit']!s:5}  mean {g['mean_pnl_pct']:+6.1f}%  "
              f"median {g['median_pnl_pct']:+6.1f}%  win {g['win_rate']*100:4.0f}%  >=+10% {g['hit_10pct']*100:4.0f}%  exits {g['exits']}")
    for lat in LATENCY_PRESETS:
        rows = sorted([g for g in grid if g["latency"] == lat], key=lambda g: -g["mean_pnl_pct"])
        g = rows[0]
        print(f"best under {lat:10s}: TP {g['tp']} SL {g['sl']} devExit {g['dev_exit']}  mean {g['mean_pnl_pct']:+.1f}%  win {g['win_rate']*100:.0f}%")
    amts = amount_sweep(tokens, dev, draws, best)
    print("\nAmount sweep (best TP/SL):")
    for a in amts:
        print(f"  {a['amount']:>5} SOL {a['latency']:10s} mean {a['mean_pnl_pct']:+6.1f}% ({a['mean_pnl_sol']:+.4f} SOL/snipe) win {a['win_rate']*100:4.0f}% buyFail {a['buy_failed']*100:.0f}%")
    lm = latency_matrix(tokens, dev, draws, best)
    print("\nLatency matrix (rows: entry slot offset L, cols: sell reaction R slots) mean PnL%:")
    for L in sorted({x["L"] for x in lm}):
        row = [x for x in lm if x["L"] == L]
        print(f"  L={L}: " + "  ".join(f"R{x['R']}:{x['mean_pnl_pct']:+6.1f}%" for x in row))
    pt = per_token(tokens, dev, draws, best)
    json.dump(dict(dev=dev, n_tokens=len(tokens), draws=draws, grid=grid, best=best, amounts=amts, latency_matrix=lm, per_token=pt),
              open(os.path.join(d, "sim_results.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
