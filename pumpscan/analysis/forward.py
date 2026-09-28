"""Forward test of frozen filters: only data that arrived AFTER the filters were frozen counts.

    python forward.py ../data/scan

The filters in frozen_rules.json were written down at `frozen_at` and are not
changed afterwards (new ideas go in as new entries with their own freeze time).
For each filter: the first time each token passes it after its freeze time is a
trade, simulated exactly like explore.py (same pair, fees, price impact, capped).
Reported with 0 and +5 min entry delay and with 6% costs, plus the running
equity curve in units (1 unit staked per trade) and its worst drawdown.
"""

import datetime as dt
import json
import os
import sys

import numpy as np

import explore as E

HERE = os.path.dirname(os.path.abspath(__file__))
EXITS = [(E.STRATS[0], 6), (E.STRATS[1], 2), (E.STRATS[4], 6)]


def mask_for(data, conds):
    m = np.ones(data.N, dtype=bool)
    for feat, op, thr in conds:
        x = data.F[feat]
        m &= (x >= thr) if op == ">=" else (x < thr) if op == "<" else (x <= thr) if op == "<=" else (x > thr)
    return m


def main(d):
    spec = json.load(open(os.path.join(HERE, "frozen_rules.json")))
    data = E.Data(d)
    ts = lambda x: dt.datetime.utcfromtimestamp(x / 1000).strftime("%m-%d %H:%M")
    print(f"data until {ts(data.end)} UTC\n")
    out = []
    for rule in spec["rules"]:
        t0 = int(dt.datetime.fromisoformat(rule.get("frozen_at", spec["frozen_at"]).replace("Z", "+00:00")).timestamp() * 1000)
        m = mask_for(data, rule["conds"]) & (data.t >= t0)
        fs = data.first_signals(m)
        print(f"{rule['name']}  (frozen {ts(t0)}; {' & '.join(f'{f}{o}{v:g}' for f, o, v in rule['conds'])})")
        res = dict(name=rule["name"], frozen=t0, exits=[])
        for st, hold in EXITS:
            r = data.returns(fs, st, hold)
            s = E.summarize(r)
            s_d = E.summarize(data.returns(fs, st, hold, delay=1))
            s_c = E.summarize(data.returns(fs, st, hold, cost=0.06))
            curve, dd = E.equity(data, fs, st, hold)
            days = {}
            ok = np.isfinite(r)
            for k, v in zip(fs[ok], r[ok]):
                days.setdefault(ts(data.t[k])[:5], []).append(v)
            per_day = "  ".join(f"{k}: {len(v)} tr {sum(v):+.1f}u" for k, v in sorted(days.items()))
            print(f"   {st[0]:24s} {hold}h  {E.fmt(s)}  maxDD {dd:+.1f}u")
            print(f"   {'':24s}     +5min delay: mean {s_d.get('mean', float('nan')):+.1%} total {s_d.get('total', 0):+.1f}u | "
                  f"6% cost: mean {s_c.get('mean', float('nan')):+.1%}   | {per_day}")
            res["exits"].append(dict(exit=st[0], hold=hold, stats=s, delay=s_d, cost6=s_c, max_dd=dd,
                                     per_day={k: [len(v), float(sum(v))] for k, v in days.items()}))
        out.append(res)
        print()
    json.dump(dict(until=data.end, rules=out), open(os.path.join(d, "forward.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    main(sys.argv[1])
