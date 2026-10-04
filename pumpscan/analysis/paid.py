"""What happens if you buy a token the moment it becomes "DEX paid" (DexScreener profile)?

    python paid.py ../data/scan

A token is DEX-paid from the first scanner cycle in which it appears in DexScreener's
latest-profiles feed (polled every 5 min; the feed holds ~30 entries and only ~2 new
ones arrive per cycle, so almost none are missed). Entry = the snapshot of that same
cycle (P0) or the first one within 10 minutes. Tokens already in the feed on the
scanner's first cycle (paid at an unknown earlier time) are left out.

Uses the wide universe (bonding-curve tokens included, liquidity estimated from the
curve) and the same trade simulation as explore.py: fees, price impact, rugs = -100%,
graduation to PumpSwap followed.
"""

import json
import os
import sys

import numpy as np

import explore as E
import forward as FW

HERE = os.path.dirname(os.path.abspath(__file__))


def line(label, data, fs, st, hold):
    s = E.summarize(data.returns(fs, st, hold))
    sd = E.summarize(data.returns(fs, st, hold, delay=1))
    if not s.get("n"):
        return f"  {label:44s} n=0"
    r = data.returns(fs, st, hold)
    r = np.sort(r[np.isfinite(r)])
    return (f"  {label:44s} {E.fmt(s)} | w/o top5 {r[:-5].sum():+6.1f}u | +5min: mean {sd.get('mean', float('nan')):+6.1%}")


def main(d):
    data = E.Data(d, wide=True)
    F = data.F
    just_paid = F["paid_min"] <= 10
    fs0 = data.first_signals(just_paid)
    st, hold = E.STRATS[0], 6
    print(f"DEX-paid tokens with a usable snapshot within 10 min of being paid: {len(fs0)}\n")

    print("=== P0: buy at the DEX-paid moment — every exit rule ===")
    for s_ in E.STRATS:
        for h in E.HOLDS:
            print(line(f"{s_[0]} {h}h", data, fs0, s_, h))

    def sub(label, extra):
        m = just_paid & extra
        return line(label, data, data.first_signals(m), st, hold)

    print(f"\n=== P0 split by what the token looked like when it got paid (exit: {st[0]}, {hold}h) ===")
    rows = [
        ("chain: solana", F["is_sol"] >= 0.5), ("chain: robinhood", F["is_sol"] < 0.5),
        ("still on pump.fun curve", F["curve"] >= 0.5), ("on pumpswap (graduated)", (F["is_pump"] >= 0.5) & (F["curve"] < 0.5)),
        ("other dex", F["is_pump"] < 0.5),
        ("age < 15 min", F["age_h"] < 0.25), ("age 15-60 min", (F["age_h"] >= 0.25) & (F["age_h"] < 1)),
        ("age 1-6 h", (F["age_h"] >= 1) & (F["age_h"] < 6)), ("age >= 6 h", F["age_h"] >= 6),
        ("MC < $15K", F["mc"] < 15e3), ("MC $15-30K", (F["mc"] >= 15e3) & (F["mc"] < 30e3)),
        ("MC $30-100K", (F["mc"] >= 30e3) & (F["mc"] < 100e3)), ("MC $100-300K", (F["mc"] >= 100e3) & (F["mc"] < 300e3)),
        ("MC >= $300K", F["mc"] >= 300e3),
        ("1h change < 0", F["ch1h"] < 0), ("1h change 0-100%", (F["ch1h"] >= 0) & (F["ch1h"] < 100)),
        ("1h change >= 100%", F["ch1h"] >= 100),
        ("24h vol < $20K", F["vol24h"] < 20e3), ("24h vol $20-100K", (F["vol24h"] >= 20e3) & (F["vol24h"] < 100e3)),
        ("24h vol >= $100K", F["vol24h"] >= 100e3),
        ("also boosted", F["boost_min"] >= 0), ("not boosted", ~(F["boost_min"] >= 0)),
    ]
    for label, extra in rows:
        print(sub(label, extra))

    print(f"\n=== P0 combined with the frozen filters (their conditions at the paid moment) ===")
    spec = json.load(open(os.path.join(HERE, "frozen_rules.json")))
    for rule in spec["rules"][1:7]:
        m = FW.mask_for(data, rule["conds"])
        print(sub(rule["name"][:44], m))

    print(f"\n=== the frozen filters, with vs without DEX paid (any time before the signal) ===")
    paid_any = F["paid_min"] >= 0
    base = (F["liq"] >= E.MIN_LIQ) & (F["vol24h"] >= E.MIN_VOL24)
    for rule in spec["rules"][:7]:
        m = FW.mask_for(data, rule["conds"]) & base
        print(line(rule["name"][:30] + " & paid", data, data.first_signals(m & paid_any), st, hold))
        print(line(rule["name"][:30] + " & NOT paid", data, data.first_signals(m & ~paid_any), st, hold))

    print("\n=== P0 over time (is it stable?) ===")
    tmid = int(np.median(data.t[fs0]))
    print(line("first half of the events", data, fs0[data.t[fs0] < tmid], st, hold))
    print(line("second half of the events", data, fs0[data.t[fs0] >= tmid], st, hold))

    # how the typical paid token moves: price relative to the paid moment
    print("\n=== median / quartiles of price vs the paid moment (tokens that did not rug) ===")
    for mins in (15, 30, 60, 120, 360):
        rs = []
        for k in fs0:
            T, P, PR, L = data.tok_series[data.tok[k]]
            i = data.loc[k]
            tgt = T[i] + mins * 60_000
            if tgt > data.end:
                continue
            m = next((n for n in range(i + 1, len(T)) if T[n] >= tgt), None)
            if m is not None and L[m] >= max(E.RUG_LIQ, E.RUG_FRAC * L[i]):
                rs.append(P[m] / P[i])
        if rs:
            q = np.quantile(rs, [0.25, 0.5, 0.75, 0.9])
            print(f"  +{mins:3d} min  n={len(rs):4d}  25%={q[0]:.2f}x  median={q[1]:.2f}x  75%={q[2]:.2f}x  90%={q[3]:.2f}x  "
                  f"share above entry {np.mean(np.array(rs) > 1):.0%}")


if __name__ == "__main__":
    main(sys.argv[1])
