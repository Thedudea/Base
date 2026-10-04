"""Export every forward-test trade of the frozen rules to CSV (one row per token signal).

    python export_trades.py ../data/scan ../results/forward_trades.csv [RULE_PREFIX ...]

Used to compare the live bot's real fills with what the simulation expected for the same tokens.
"""

import csv
import datetime as dt
import gzip
import glob
import json
import os
import sys

import numpy as np

import explore as E
import forward as FW

HERE = os.path.dirname(os.path.abspath(__file__))


def names(d):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*", "*.ndjson.gz"))):
        try:
            with gzip.open(f, "rt") as fh:
                for line in fh:
                    if line.startswith('["m"'):
                        r = json.loads(line)
                        out[(r[2], r[3])] = (r[4], r[5])
        except (OSError, EOFError, ValueError):
            continue
    return out


def main(d, out_path, prefixes):
    spec = json.load(open(os.path.join(HERE, "frozen_rules.json")))
    data = E.Data(d, wide=True)
    nm = names(d)
    strats = [(E.STRATS[0], 6, "ret_half2x_trail35_sl35_6h"), (E.STRATS[3], 6, "ret_all2x_sl35_6h")]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rule", "chain", "token", "name", "symbol", "signal_utc", "age_min", "mc", "ath_mc_seen", "liq", "vol1h",
                    "vol24h", "tx1h", "tx24h", "dex_paid", "rug_within_6h"] + [s[2] for s in strats] +
                   [s[2] + "_delay5m" for s in strats])
        for rule in spec["rules"]:
            if prefixes and not any(rule["name"].startswith(p + " ") for p in prefixes):
                continue
            t0 = int(dt.datetime.fromisoformat(rule.get("frozen_at", spec["frozen_at"]).replace("Z", "+00:00")).timestamp() * 1000)
            conds = rule["conds"] if rule.get("universe") == "wide" else [["liq", ">=", E.MIN_LIQ], ["vol24h", ">=", E.MIN_VOL24]] + rule["conds"]
            fs = data.first_signals(FW.mask_for(data, conds) & (data.t >= t0))
            rets = [data.returns(fs, s, h) for s, h, _ in strats]
            rets_d = [data.returns(fs, s, h, delay=1) for s, h, _ in strats]
            for n, k in enumerate(fs):
                if not np.isfinite(rets[0][n]):
                    continue
                key = data.tok_name[data.tok[k]]
                T, P, PR, L = data.tok_series[data.tok[k]]
                i = data.loc[k]
                rug = any(L[m] < max(E.RUG_LIQ, E.RUG_FRAC * L[i]) for m in range(i + 1, len(T))
                          if PR[m] == PR[i] and T[m] - T[i] <= 6 * E.HOUR)
                F = data.F
                g = lambda f: "" if not np.isfinite(F[f][k]) else round(float(F[f][k]), 2)
                w.writerow([rule["name"].split()[0], key[0], key[1], *(nm.get(key, ("", ""))),
                            dt.datetime.utcfromtimestamp(data.t[k] / 1000).strftime("%Y-%m-%d %H:%M"),
                            "" if not np.isfinite(F["age_h"][k]) else round(float(F["age_h"][k]) * 60),
                            g("mc"), g("mc_high"), g("liq"), g("vol1h"), g("vol24h"), g("tx1h"), g("tx24h"),
                            int(F["paid_min"][k] >= 0) if np.isfinite(F["paid_min"][k]) else 0, int(rug)]
                           + [round(float(r[n]), 4) for r in rets]
                           + ["" if not np.isfinite(r[n]) else round(float(r[n]), 4) for r in rets_d])
    print("wrote", out_path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
