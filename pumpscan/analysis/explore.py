"""Walk-forward search for GMGN-style entry filters and exit rules on the scanner data.

    python explore.py ../data/scan [train_frac=0.6]

Unit of analysis: the FIRST moment a token passes a filter — the moment it would
pop up in a filtered GMGN list. From there a trade is simulated on the same
pair's later snapshots (5 min for active tokens, 30 min for quiet ones) with
fees, price impact for the position size, a cap on single-trade profit, and
optionally one snapshot of entry delay.

Filters are searched (beam search over threshold conditions) only on the earlier
part of the data (train) and then scored on the later part they never saw
(test). Only the test numbers say anything about the future.
"""

import bisect
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

from learn import rows

HOUR = 3600_000
MIN_LIQ = 5000
CAP = 9.0  # one trade can add at most +900%
SIZE = 150.0  # position size in USD, for price impact
COST = 0.03  # fees + priority/tip, round trip

# (name, tp_fraction, tp_multiple, trail, trail_before_tp, stop_ratio, hold_hours)
STRATS = [
    ("half@2x trail35 sl35", 0.5, 2.0, 0.35, False, 0.65),
    ("half@1.5x trail30 sl30", 0.5, 1.5, 0.30, False, 0.70),
    ("all@1.5x sl25", 1.0, 1.5, None, False, 0.75),
    ("all@2x sl35", 1.0, 2.0, None, False, 0.65),
    ("trail40 sl40", 0.0, None, 0.40, True, 0.60),
    ("trail25 sl25", 0.0, None, 0.25, True, 0.75),
    ("half@3x trail40 sl40", 0.5, 3.0, 0.40, False, 0.60),
    ("half@2x trail35 no-sl", 0.5, 2.0, 0.35, False, 0.0),
]
HOLDS = [2, 6]

GMGN_FEATS = ["age_h", "mc", "mc_high", "liq", "vol24h", "tx24h", "holders", "top10", "devpct", "is_sol"]
VISIBLE_FEATS = ["ch1h", "vol1h", "tx1h", "ch5m"]  # columns you can read off GMGN's Trending list (1h view)
ALL_FEATS = GMGN_FEATS + VISIBLE_FEATS + [
    "liq_mc", "vol5m", "wake", "wake6", "accel", "buy_ratio5m", "buy_ratio1h", "avg_trade", "ch6h", "ch24h",
    "pairs", "socials", "dd_high", "hours_tracked", "buyers1h", "buyer_ratio", "nlists", "best_rank", "boosted"]


def r2(x):
    """Round to 2 significant digits so thresholds are typeable into GMGN."""
    if not x or not math.isfinite(x):
        return x
    return round(x, -int(math.floor(math.log10(abs(x)))) + 1)


class Data:
    def __init__(self, d):
        sch = json.load(open(os.path.join(d, "schema.json")))
        # older data folders predate the holder rows
        sch.setdefault("h", ["type", "t", "chain", "token", "holders", "top10", "top11_20", "top21_40", "rest", "devPct",
                             "mintAuth", "freezeAuth", "honeypot", "gtScore", "launchpadDone", "holdersUpdated"])
        ix = {k: {c: i for i, c in enumerate(v)} for k, v in sch.items()}
        self.ix = ix
        D, G, H, B, M = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), {}
        for r in rows(d):
            ty = r[0]
            if ty == "d":
                D[(r[2], r[3])].append(r)
            elif ty == "g":
                G[(r[2], r[3])].append(r)
            elif ty == "h":
                H[(r[2], r[3])].append(r)
            elif ty == "b":
                B[(r[2], r[3])].append(r[1])
            elif ty == "m":
                M[(r[2], r[3])] = r
        self.end = max(r[1] for s in D.values() for r in s[-1:])
        self.start = min(r[1] for s in D.values() for r in s[:1])
        feats = defaultdict(list)
        self.tok_series = []  # per token: (T, P, pair_code, L)
        where_tok, where_i, tok_name = [], [], []
        di, gi, hi = ix["d"], ix["g"], ix["h"]
        pair_codes = {}
        for tk, key in enumerate(sorted(D)):
            s = sorted(D[key], key=lambda r: r[1])
            T = np.array([r[1] for r in s], dtype=np.int64)
            P = np.array([r[di["price"]] or 0.0 for r in s], dtype=float)
            PR = np.array([pair_codes.setdefault(r[di["pair"]], len(pair_codes)) for r in s], dtype=np.int64)
            L = np.array([r[di["liq"]] or 0.0 for r in s], dtype=float)
            self.tok_series.append((T, P, PR, L))
            tok_name.append(key)
            g = sorted(G.get(key, []), key=lambda r: r[1])
            gt = [r[1] for r in g]
            h = sorted(H.get(key, []), key=lambda r: r[1])
            ht = [r[1] for r in h]
            bt = sorted(B.get(key, []))
            m = M.get(key)
            socials = len(m[ix["m"]["socials"]] or []) if m else 0
            high = {}
            mc_high = 0.0
            for i, r in enumerate(s):
                t = r[1]
                price, liq = P[i], L[i]
                pc = PR[i]
                prev_high = high.get(pc, 0.0)
                high[pc] = max(prev_high, price)
                mc = r[di["mc"]] or r[di["fdv"]] or 0.0
                mc_high = max(mc_high, mc)
                if liq < MIN_LIQ or price <= 0:
                    continue
                v1 = r[di["volAllH1"]] or r[di["volH1"]] or 0.0
                v5 = r[di["volM5"]] or 0.0
                v6 = r[di["volH6"]] or 0.0
                v24 = r[di["volAllH24"]] or r[di["volH24"]] or 0.0
                b5, s5 = r[di["buysM5"]] or 0, r[di["sellsM5"]] or 0
                b1, s1 = r[di["buysH1"]] or 0, r[di["sellsH1"]] or 0
                tx24 = (r[di["buysH24"]] or 0) + (r[di["sellsH24"]] or 0)
                # GeckoTerminal list rows seen in the last hour
                k1 = bisect.bisect_right(gt, t)
                k0 = bisect.bisect_left(gt, t - HOUR)
                recent = g[k0:k1]
                last_g = recent[-1] if recent else None
                ranks = [x[gi["rank"]] for x in recent if x[gi["list"]] != "new"]
                # latest holder stats (valid for 3h)
                kh = bisect.bisect_right(ht, t)
                hh = h[kh - 1] if kh and t - ht[kh - 1] <= 3 * HOUR else None
                kb = bisect.bisect_right(bt, t)
                boosted = 1.0 if kb and t - bt[kb - 1] <= 6 * HOUR else 0.0
                buyers = last_g[gi["buyersH1"]] if last_g else None
                sellers = last_g[gi["sellersH1"]] if last_g else None
                f = dict(
                    age_h=(t / 1000 - r[di["pairCreated"]]) / 3600 if r[di["pairCreated"]] else np.nan,
                    mc=mc, mc_high=mc_high, liq=liq, liq_mc=liq / mc if mc else np.nan,
                    vol5m=v5, vol1h=v1, vol24h=v24, tx1h=b1 + s1, tx24h=tx24,
                    wake=v1 / max(1.0, (v24 - v1) / 23), wake6=(r[di["volH1"]] or 0) / max(1.0, v6 / 6),
                    accel=v5 * 12 / max(1.0, v1), buy_ratio5m=b5 / max(1, b5 + s5), buy_ratio1h=b1 / max(1, b1 + s1),
                    avg_trade=v1 / max(1, b1 + s1),
                    ch5m=r[di["chM5"]] if r[di["chM5"]] is not None else np.nan,
                    ch1h=r[di["chH1"]] if r[di["chH1"]] is not None else np.nan,
                    ch6h=r[di["chH6"]] if r[di["chH6"]] is not None else np.nan,
                    ch24h=r[di["chH24"]] if r[di["chH24"]] is not None else np.nan,
                    pairs=r[di["nPairs"]] or 1, socials=socials,
                    dd_high=price / prev_high if prev_high > 0 else 1.0,
                    hours_tracked=(t - s[0][1]) / HOUR,
                    buyers1h=buyers if buyers is not None else np.nan,
                    buyer_ratio=buyers / max(1, buyers + sellers) if buyers is not None and sellers is not None else np.nan,
                    nlists=len({x[gi["list"]] for x in recent}), best_rank=min(ranks) if ranks else np.nan,
                    boosted=boosted,
                    holders=hh[hi["holders"]] if hh and hh[hi["holders"]] is not None else np.nan,
                    top10=hh[hi["top10"]] if hh and hh[hi["top10"]] is not None else np.nan,
                    devpct=hh[hi["devPct"]] if hh and hh[hi["devPct"]] is not None else np.nan,
                    is_sol=1.0 if key[0] == "solana" else 0.0,
                )
                for k, v in f.items():
                    feats[k].append(np.nan if v is None else float(v))
                feats["t"].append(float(t))
                where_tok.append(tk)
                where_i.append(i)
        self.F = {k: np.array(v, dtype=float) for k, v in feats.items()}
        self.t = self.F.pop("t").astype(np.int64)
        self.tok = np.array(where_tok, dtype=np.int64)
        self.loc = np.array(where_i, dtype=np.int64)
        self.tok_name = tok_name
        self.N = len(self.t)
        self.cache = {}

    # ------------------------------------------------------------- trading

    def sim(self, k, strat, hold_h, delay=0, cost=COST, size=SIZE):
        T, P, PR, L = self.tok_series[self.tok[k]]
        i = self.loc[k]
        t0, pair = T[i], PR[i]
        hold = hold_h * HOUR
        if t0 + hold > self.end:
            return None  # outcome not known yet
        j = i
        if delay:
            j = i + 1
            while j < len(T) and PR[j] != pair:
                j += 1
            if j >= len(T) or T[j] - t0 > 12 * 60_000:
                return None  # could not get in within ~2 snapshots
        _, tp_frac, tp_x, trail, trail_always, sl = strat
        pe = P[j]
        imp = lambda usd, liq: min(0.5, 2 * usd / liq) if liq > 0 else 0.5
        imp_in = imp(size, L[j])
        realized, remaining, peak, tp_done = 0.0, 1.0, 1.0, False
        last_r, last_l = 1.0, L[j]
        for m in range(j + 1, len(T)):
            if T[m] - T[j] > hold:
                break
            if PR[m] != pair or P[m] <= 0:
                continue
            r = P[m] / pe
            last_r, last_l = r, L[m]
            peak = max(peak, r)
            if tp_x and not tp_done and r >= tp_x:
                realized += tp_frac * r * (1 - imp(size * tp_frac * r, L[m]))
                remaining -= tp_frac
                tp_done = True
                if remaining <= 1e-9:
                    break
            stop = sl
            if trail and (tp_done or trail_always):
                stop = max(stop, peak * (1 - trail))
            if r <= stop:
                realized += remaining * r * (1 - imp(size * remaining * r, L[m]))
                remaining = 0.0
                break
        if remaining > 0:
            realized += remaining * last_r * (1 - imp(size * remaining * last_r, last_l))
        return min(realized / (1 + imp_in) - 1 - cost, CAP)

    def returns(self, idx, strat, hold_h, delay=0, cost=COST, size=SIZE):
        key = (strat[0], hold_h, delay, cost, size)
        c = self.cache.get(key)
        if c is None:
            c = self.cache[key] = np.full(self.N, np.nan)
            self.cache[key + ("done",)] = np.zeros(self.N, dtype=bool)
        done = self.cache[key + ("done",)]
        for k in idx[~done[idx]]:
            v = self.sim(k, strat, hold_h, delay, cost, size)
            c[k] = np.nan if v is None else v
            done[k] = True
        return c[idx]

    def first_signals(self, mask):
        idx = np.flatnonzero(mask)
        if not len(idx):
            return idx
        _, pos = np.unique(self.tok[idx], return_index=True)
        return np.sort(idx[pos])


def summarize(r):
    r = r[np.isfinite(r)]
    n = len(r)
    if not n:
        return dict(n=0)
    srt = np.sort(r)
    trim = srt[: max(1, int(n * 0.95))]
    return dict(n=n, mean=float(r.mean()), med=float(np.median(r)), win=float((r > 0).mean()), total=float(r.sum()),
                trim_mean=float(trim.mean()), lcb=float(r.mean() - r.std(ddof=1) / math.sqrt(n)) if n > 1 else -1.0,
                pump=float((r > 0.5).mean()))


def fmt(s):
    if not s.get("n"):
        return "n=0"
    return (f"n={s['n']:4d} mean={s['mean']:+6.1%} med={s['med']:+6.1%} win={s['win']:4.0%} "
            f"trim95={s['trim_mean']:+6.1%} total={s['total']:+7.1f}u")


class Cond:
    def __init__(self, feat, op, thr, data):
        self.feat, self.op, self.thr = feat, op, thr
        x = data.F[feat]
        self.mask = (x >= thr) if op == ">=" else (x < thr)

    def __repr__(self):
        v = self.thr
        if self.feat in ("mc", "mc_high", "liq", "vol24h", "vol1h", "vol5m", "avg_trade"):
            v = f"${v / 1e3:g}K" if v < 1e6 else f"${v / 1e6:g}M"
        return f"{self.feat}{self.op}{v}"


def conditions(data, feats, train_mask):
    out = []
    for f in feats:
        x = data.F[f][train_mask]
        x = x[np.isfinite(x)]
        if len(x) < 100:
            continue
        thr = sorted({r2(q) for q in np.quantile(x, np.linspace(0.1, 0.9, 9))})
        if f == "is_sol":
            thr = [0.5]
        for v in thr:
            out.append(Cond(f, ">=", v, data))
            out.append(Cond(f, "<", v, data))
    return out


def evaluate(data, mask, strat, hold_h, part, split, delay=0, cost=COST, size=SIZE):
    fs = data.first_signals(mask)
    fs = fs[(data.t[fs] < split) if part == "train" else (data.t[fs] >= split)]
    r = data.returns(fs, strat, hold_h, delay, cost, size)
    return summarize(r), fs[np.isfinite(r)]


def beam_search(data, conds, strat, hold_h, split, depth=4, width=10, min_n=30):
    base = np.ones(data.N, dtype=bool)
    beam = [((), base)]
    found = {}
    for _ in range(depth):
        cand = []
        for rule, mask in beam:
            used = {(c.feat, c.op) for c in rule}
            for c in conds:
                if (c.feat, c.op) in used:
                    continue
                m2 = mask & c.mask
                s, _ = evaluate(data, m2, strat, hold_h, "train", split)
                if s.get("n", 0) < min_n:
                    continue
                cand.append((s["lcb"], tuple(sorted(rule + (c,), key=repr)), m2, s))
        uniq = {}
        for sc, rule, m2, s in sorted(cand, key=lambda x: -x[0]):
            uniq.setdefault(repr(rule), (sc, rule, m2, s))
        top = list(uniq.values())[:width]
        beam = [(rule, m2) for _, rule, m2, _ in top]
        for sc, rule, m2, s in top:
            found[repr(rule)] = (sc, rule, m2, s)
    return sorted(found.values(), key=lambda x: -x[0])


def equity(data, fs, strat, hold_h):
    r = data.returns(fs, strat, hold_h)
    ok = np.isfinite(r)
    order = np.argsort(data.t[fs][ok])
    curve = np.cumsum(r[ok][order])
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return curve, float((curve - peak).min()) if len(curve) else 0.0


def main(d, train_frac=0.6):
    data = Data(d)
    hold_max = max(HOLDS) * HOUR
    split = int(data.start + train_frac * (data.end - hold_max - data.start))
    ts = lambda x: __import__("datetime").datetime.utcfromtimestamp(x / 1000).strftime("%m-%d %H:%M")
    print(f"snapshots usable: {data.N}, tokens {len(data.tok_series)}, data {ts(data.start)} .. {ts(data.end)} UTC, "
          f"train < {ts(split)} <= test")
    train_mask = data.t < split
    out = dict(split=split, start=data.start, end=data.end, results=[])

    everything = np.ones(data.N, dtype=bool)
    print("\nbaseline: every token, first time seen")
    for st in STRATS[:2]:
        for hold in HOLDS:
            print(f"  {st[0]:24s} {hold}h  train {fmt(evaluate(data, everything, st, hold, 'train', split)[0])}")
            print(f"  {'':24s}     test  {fmt(evaluate(data, everything, st, hold, 'test', split)[0])}")

    for label, feats in (("GMGN filter fields only", GMGN_FEATS), ("GMGN fields + what the Trending list shows", GMGN_FEATS + VISIBLE_FEATS),
                         ("all features", ALL_FEATS)):
        conds = conditions(data, feats, train_mask)
        for st, hold in ((STRATS[0], 6), (STRATS[1], 2), (STRATS[4], 6)):
            print(f"\n=== search: {label} | exit {st[0]} | hold {hold}h | {len(conds)} conditions ===")
            found = beam_search(data, conds, st, hold, split)
            for sc, rule, m, s in found[:6]:
                te, fs_te = evaluate(data, m, st, hold, "test", split)
                d1, _ = evaluate(data, m, st, hold, "test", split, delay=1)
                c6, _ = evaluate(data, m, st, hold, "test", split, cost=0.06)
                print(f"  {' & '.join(map(repr, rule))}")
                print(f"      train {fmt(s)}")
                print(f"      TEST  {fmt(te)}   | +5min delay: {fmt(d1)} | 6% cost: mean {c6.get('mean', float('nan')):+.1%}")
                out["results"].append(dict(search=label, exit=st[0], hold=hold, rule=list(map(repr, rule)), train=s, test=te,
                                           test_delay=d1, test_cost6=c6))

    json.dump(out, open(os.path.join(d, "explore.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0.6)
