"""Counterfactual: *you* launch each of a dev's pump.fun tokens, transparently.

Replays the real outside order flow of each launch against a bonding curve that
starts with your own dev buy instead of the original dev's:
  - the original dev's trades are replaced by yours;
  - trades of the dev's funded bait wallets are removed (no fake buyers);
  - outside buys are replayed by SOL amount; outside sells sell the same fraction
    of the seller's holding as they did historically;
  - "discovery" buyers (after the first `bot_slots` slots) are kept with
    probability `demand` to model launching later than / competing with others;
  - your sells land `react` slots after the trade that triggered them;
  - costs: create overhead, per-sell tx cost, pump.fun protocol fee; creator fees
    (paid by everyone, including you) are income because you are the creator.

    python sim_launch.py ../data/v5-bwam/<dev> [draws]
"""

import itertools
import json
import os
import random
import statistics as st
import sys

from parse import load_dev
from simulate import Curve

CREATE_OVERHEAD = 0.02  # rent + tx fee + tip for the create tx (measured)
SELL_TX_COST = 0.002  # fee + tip per sell tx (measured)


def tokens_for_sol(curve, sol_out):
    """Tokens to sell to receive `sol_out` lamports from the curve (before fees)."""
    if sol_out >= curve.vsr:
        return None
    return int(sol_out * curve.vtr / (curve.vsr - sol_out)) + 1


def run_token(tok, dev, bait, B, rule, demand, react, bot_slots, rng):
    tr = tok["trades"]
    cs = tok["creation"]["slot"]
    first = tr[0]
    fee_p = first["fee_bps"] / 1e4
    fee_c = first["cfee_bps"] / 1e4
    curve = Curve(first["pre_vsr"], first["pre_vtr"])
    lam = int(B * 1e9)
    sol_in = int(lam / (1 + fee_p + fee_c))
    pos = curve.buy_sol(sol_in)
    cost = B + CREATE_OVERHEAD
    creator_income = sol_in * fee_c / 1e9  # our own creator fee comes back
    proceeds = 0.0
    n_sells = 0
    hist, cf = {}, {}  # historical / counterfactual outsider holdings
    pending = []  # (land_slot, kind, amount)
    kind, a, b = rule  # ("time", T, _) | ("mirror", k, T) | ("target", X, T)
    T = a if kind == "time" else b
    pending.append((cs + T, "all", 0))
    basis_price = sol_in / pos

    def sell(qty):
        nonlocal pos, proceeds, n_sells, creator_income
        qty = min(qty, pos)
        if qty <= 0:
            return
        out = curve.sell_tok(qty)
        pos -= qty
        proceeds += out * (1 - fee_p - fee_c) / 1e9
        creator_income += out * fee_c / 1e9
        n_sells += 1

    def flush(upto_slot):
        for p in sorted(pending):
            if p[0] <= upto_slot and pos > 0:
                pending.remove(p)
                if p[1] == "all":
                    sell(pos)
                else:
                    q = tokens_for_sol(curve, p[2])
                    sell(q if q else pos)

    for t in tr:
        if t["in_create_tx"] or t["user"] == dev or t["user"] in bait:
            continue
        flush(t["slot"] - 1)  # our txs that landed in earlier slots
        if pos <= 0 and not pending:
            break
        w = t["user"]
        off = t["slot"] - cs
        if t["is_buy"]:
            if off > bot_slots and rng.random() > demand:
                continue
            got = curve.buy_sol(t["sol"])
            creator_income += t["sol"] * fee_c / 1e9
            cf[w] = cf.get(w, 0) + got
            hist[w] = hist.get(w, 0) + t["tok"]
            if kind == "mirror" and pos > 0:
                pending.append((t["slot"] + react, "sol", t["sol"] * a))
            if kind == "target" and pos > 0:
                value = pos * curve.price * (1 - fee_p)
                if value >= (1 + a) * sol_in * (1 + fee_p):
                    pending.append((t["slot"] + react, "all", 0))
        else:
            if w not in cf or hist.get(w, 0) <= 0:
                continue
            frac = min(1.0, t["tok"] / hist[w])
            q = int(cf[w] * frac)
            out = curve.sell_tok(q)
            creator_income += out * fee_c / 1e9
            cf[w] -= q
            hist[w] -= t["tok"]
    flush(10**12)
    if pos > 0:
        sell(pos)
    pnl = proceeds + creator_income - cost - n_sells * SELL_TX_COST
    return pnl, n_sells


def sweep(tokens, dev, bait, draws):
    rules = [("time", 5, 0), ("time", 10, 0), ("time", 20, 0), ("time", 40, 0),
             ("mirror", 1.0, 30), ("mirror", 2.6, 30), ("mirror", 2.6, 60), ("mirror", 5.0, 30),
             ("target", 0.10, 30), ("target", 0.25, 40), ("target", 0.50, 60)]
    out = []
    for B, rule, demand, react in itertools.product([1, 3, 5, 12], rules, [1.0, 0.5, 0.25], [1, 3]):
        rng = random.Random(11)
        per_tok = []
        for tok in tokens:
            vals = [run_token(tok, dev, bait, B, rule, demand, react, 3, rng)[0] for _ in range(draws if demand < 1 else 1)]
            per_tok.append(sum(vals) / len(vals))
        total = sum(per_tok)
        srt = sorted(per_tok, reverse=True)
        acc = pk = dd = 0
        for v in per_tok:
            acc += v
            pk = max(pk, acc)
            dd = min(dd, acc - pk)
        out.append(dict(B=B, rule=f"{rule[0]}:{rule[1]}:{rule[2]}", demand=demand, react=react, total=total,
                        per_launch=total / len(per_tok), win=sum(1 for v in per_tok if v > 0) / len(per_tok),
                        median=st.median(per_tok), without_top10=sum(srt[10:]), max_dd=dd,
                        best=srt[0], worst=srt[-1]))
    return out


def main(d, draws=20):
    dev = os.path.basename(os.path.normpath(d))
    deep = json.load(open(os.path.join(d, "deep.json")))
    bait = set(deep["cluster"]) - {dev}
    tokens = [t for t in load_dev(d) if t["trades"] and t["trades"][0]["in_create_tx"]
              and t["trades"][0]["user"] == dev and t["trades"][0]["pre_vsr"] == 30_000_000_000]
    # reference: what the real group made on exactly these launches
    ref = {t["mint"]: t for t in deep["tokens"]}
    real = sum(ref[t["mint"]]["cluster_sol"] for t in tokens if t["mint"] in ref)
    hours = (max(t["creation"]["blockTime"] for t in tokens) - min(t["creation"]["blockTime"] for t in tokens)) / 3600
    print(f"{len(tokens)} standard pump.fun launches over {hours:.1f} h; real group PnL on these: {real:+.1f} SOL")
    res = sweep(tokens, dev, bait, draws)
    json.dump(dict(n=len(tokens), hours=hours, real_group=real, results=res),
              open(os.path.join(d, "sim_launch.json"), "w"), indent=1)
    for demand in (1.0, 0.5, 0.25):
        for react in (1, 3):
            rows = sorted([r for r in res if r["demand"] == demand and r["react"] == react], key=lambda r: -r["total"])
            print(f"\n demand {demand:.0%}, react {react} slots — best 6:")
            for r in rows[:6]:
                print(f"   B={r['B']:>2} {r['rule']:16s} total {r['total']:+7.1f} per launch {r['per_launch']:+.3f} "
                      f"win {r['win']:.0%} median {r['median']:+.3f} w/o top10 {r['without_top10']:+7.1f} maxDD {r['max_dd']:+.1f}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 20)
