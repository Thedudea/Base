"""Sensitivity of the best simple TP/SL settings to the assumptions we cannot observe.

    python sensitivity.py ../data/v3/<dev> [draws]
"""

import json
import os
import sys

from parse import load_dev
from simulate import LATENCY_PRESETS, Costs, Strategy, run_many, summarize


def row(tokens, dev, draws, amount, tp, sl, lat, costs=None, **kw):
    strat = Strategy(amount_sol=amount, levels=[(tp, 100), (sl, 100)], **kw)
    s = summarize(run_many(tokens, strat, LATENCY_PRESETS[lat], costs or Costs(), n_draws=draws, dev=dev), amount)
    return s


def main():
    d = sys.argv[1]
    draws = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    dev = os.path.basename(os.path.normpath(d))
    tokens = [t for t in load_dev(d) if t["trades"]]
    out = {}

    def show(name, s):
        out[name] = s
        filled = 1 - s["buy_failed"]
        cond = (s["mean_pnl_pct"] + s["buy_failed"] * 0.5) / filled if filled else 0
        print(f"  {name:58s} mean {s['mean_pnl_pct']:+6.1f}%  if-filled {cond:+6.1f}%  win {s['win_rate']*100:3.0f}%  "
              f"p10 {s['p10_pct']:+6.1f}%  p90 {s['p90_pct']:+6.1f}%  buyFail {s['buy_failed']*100:3.0f}%")

    print(f"== {dev}  ({len(tokens)} tokens, {draws} draws each)")
    print("buy slippage (GMGN 'Auto' is unknown), 0.5 SOL, TP15/SL-25, typical latency:")
    for slip in (15, 30, 50, 100):
        for ref in ("create", "bundle"):
            show(f"slip {slip}% ref={ref}", row(tokens, dev, draws, 0.5, 15, -25, "typical", buy_slippage_pct=slip, slip_ref=ref))
    print("costs, 0.5 SOL, TP15/SL-25, typical latency, slip 50% ref=bundle:")
    base = dict(buy_slippage_pct=50, slip_ref="bundle")
    show("default (prio 0.00045, tip 0.001, GMGN 1%, rent kept)", row(tokens, dev, draws, 0.5, 15, -25, "typical", **base))
    show("rent refunded (account closed after sell)", row(tokens, dev, draws, 0.5, 15, -25, "typical", Costs(rent_refunded=True), **base))
    show("no tip, prio 0.0001, rent refunded", row(tokens, dev, draws, 0.5, 15, -25, "typical",
                                                  Costs(rent_refunded=True, tip_sol=0.0, priority_fee_sol=0.0001), **base))
    show("zero platform fee (upper bound)", row(tokens, dev, draws, 0.5, 15, -25, "typical",
                                                Costs(rent_refunded=True, gmgn_fee_rate=0.0), **base))
    print("TP/SL around the target (0.5 SOL, slip 50% ref=bundle, rent refunded):")
    for lat in ("optimistic", "typical", "slow"):
        for tp, sl in ((10, -15), (10, -25), (15, -25), (20, -25), (20, -35)):
            for dev_exit in (False, True):
                show(f"{lat:10s} TP{tp} SL{sl} devExit={dev_exit}",
                     row(tokens, dev, draws, 0.5, tp, sl, lat, Costs(rent_refunded=True), exit_on_dev_sell=dev_exit, **base))
    json.dump(out, open(os.path.join(d, "sensitivity.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
