"""Learn which visible stats preceded pumps, from the scanner's snapshots.

    python learn.py ../data/scan [horizon_hours=24]

Decision points: the first snapshot of each token in each hour (what you would
see scrolling GMGN / DexScreener at that moment). Outcome over the next
`horizon` hours from the 5-minute price snapshots:
  pump   = price reached 2x before it fell to 0.5x
  ret    = simple trade: buy at the snapshot, sell half at 2x, rest on a 35%
           trailing stop / at -35% / at the horizon; 3% round-trip cost
Then: base rates, pump rate by feature bucket, and a threshold search for
GMGN-style filters (age, MC, liquidity, 24h volume, volume wake-up, ...).
"""

import glob
import gzip
import itertools
import json
import math
import os
import sys
from collections import defaultdict

HOUR = 3600_000


def rows(d):
    for f in sorted(glob.glob(os.path.join(d, "*", "*.ndjson.gz"))):
        try:
            with gzip.open(f, "rt") as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)
        except (OSError, EOFError, ValueError):
            continue  # a half-written last member


def load(d):
    sch = json.load(open(os.path.join(d, "schema.json")))
    idx = {k: {c: i for i, c in enumerate(v)} for k, v in sch.items()}
    series = defaultdict(list)  # (chain, token) -> [dict]
    meta, lists, boosts = {}, defaultdict(list), defaultdict(list)
    for r in rows(d):
        ty = r[0]
        if ty == "d":
            series[(r[2], r[3])].append({c: r[i] for c, i in idx["d"].items()})
        elif ty == "m":
            meta[(r[2], r[3])] = {c: r[i] for c, i in idx["m"].items()}
        elif ty == "g":
            lists[(r[2], r[3])].append({c: r[i] for c, i in idx["g"].items()})
        elif ty == "b":
            boosts[(r[2], r[3])].append(r[1])
    for s in series.values():
        s.sort(key=lambda x: x["t"])
    for s in lists.values():
        s.sort(key=lambda x: x["t"])
    return series, meta, lists, boosts


def outcome(s, i, horizon_ms, cost=0.03):
    p0 = s[i]["price"]
    t0 = s[i]["t"]
    if not p0:
        return None
    fut = [x for x in s[i + 1:] if x["t"] - t0 <= horizon_ms and x["price"]]
    # need coverage to the horizon, unless the token was dropped for being dead
    last_t = s[-1]["t"]
    if last_t - t0 < horizon_ms * 0.9 and not (fut and fut[-1]["price"] < 0.3 * p0):
        return None
    pump = False
    for x in fut:
        r = x["price"] / p0
        if r <= 0.5:
            break
        if r >= 2:
            pump = True
            break
    mx = max((x["price"] for x in fut), default=p0) / p0
    # the simple trade
    half_sold = False
    realized = 0.0
    peak = 1.0
    ret = None
    for x in fut:
        r = x["price"] / p0
        peak = max(peak, r)
        if not half_sold and r >= 2:
            realized += 0.5 * r
            half_sold = True
        stop = max(0.65, peak * 0.65) if half_sold else 0.65
        if r <= stop:
            ret = realized + (0.5 if half_sold else 1.0) * r
            break
    if ret is None:
        r = fut[-1]["price"] / p0 if fut else 1.0
        ret = realized + (0.5 if half_sold else 1.0) * r
    return dict(pump=pump, max=mx, ret=ret - 1 - cost)


def features(x, m, s_lists, t):
    age_h = (x["t"] / 1000 - x["pairCreated"]) / 3600 if x["pairCreated"] else None
    v1, v6, v24 = x["volAllH1"] or x["volH1"] or 0, x["volH6"] or 0, x["volAllH24"] or x["volH24"] or 0
    base = max(1.0, (v24 - v1) / 23)
    mc = x["mc"] or x["fdv"]
    recent = [g for g in s_lists if t - HOUR <= g["t"] <= t]
    g = recent[-1] if recent else None
    return dict(
        chain=x["chain"], age_h=age_h, mc=mc, liq=x["liqAll"] or x["liq"], liq_mc=(x["liqAll"] or 0) / mc if mc else None,
        vol1h=v1, vol24h=v24, wake=v1 / base, wake6=(x["volH1"] or 0) / max(1.0, v6 / 6),
        tx1h=(x["buysH1"] or 0) + (x["sellsH1"] or 0), tx24h=(x["buysH24"] or 0) + (x["sellsH24"] or 0),
        buy_ratio1h=(x["buysH1"] or 0) / max(1, (x["buysH1"] or 0) + (x["sellsH1"] or 0)),
        ch5m=x["chM5"], ch1h=x["chH1"], ch6h=x["chH6"], ch24h=x["chH24"], pairs=x["nPairs"],
        socials=len((m or {}).get("socials") or []), website=int(bool((m or {}).get("nWebsites"))),
        buyers1h=g["buyersH1"] if g else None, sellers1h=g["sellersH1"] if g else None,
        in_list=int(bool(recent)), dex=x["dex"],
    )


def decision_points(series, meta, lists, horizon_ms):
    pts = []
    for k, s in series.items():
        seen_hours = set()
        for i, x in enumerate(s):
            hb = x["t"] // HOUR
            if hb in seen_hours:
                continue
            seen_hours.add(hb)
            o = outcome(s, i, horizon_ms)
            if o is None:
                continue
            f = features(x, meta.get(k), lists.get(k, []), x["t"])
            pts.append(dict(token=k[1], t=x["t"], **f, **o))
    return pts


def stats(pts):
    n = len(pts)
    if not n:
        return dict(n=0)
    toks = len({p["token"] for p in pts})
    pr = sum(p["pump"] for p in pts) / n
    rets = sorted(p["ret"] for p in pts)
    return dict(n=n, tokens=toks, pump=pr, ret=sum(rets) / n, med=rets[n // 2], p10=rets[n // 10])


def fmt(st):
    if not st.get("n"):
        return "n=0"
    return (f"n={st['n']:5d} tok={st['tokens']:4d} pump2x={st['pump']:6.1%} avg_ret={st['ret']:+6.1%} "
            f"median={st['med']:+6.1%} p10={st['p10']:+6.1%}")


def buckets(pts, feat, edges):
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [p for p in pts if p.get(feat) is not None and lo <= p[feat] < hi]
        out.append((lo, hi, stats(sel)))
    return out


FEATS = {
    "age_h": [0, 1, 6, 24, 72, 168, 1e9],
    "mc": [0, 30e3, 100e3, 300e3, 1e6, 3e6, 10e6, 1e12],
    "liq": [0, 10e3, 30e3, 100e3, 300e3, 1e12],
    "liq_mc": [0, 0.05, 0.1, 0.2, 0.4, 1e9],
    "vol24h": [0, 10e3, 50e3, 200e3, 1e6, 5e6, 1e12],
    "wake": [0, 0.5, 1, 2, 5, 10, 1e9],
    "wake6": [0, 0.5, 1, 2, 4, 1e9],
    "ch1h": [-100, -20, 0, 20, 50, 100, 1e9],
    "ch24h": [-100, -50, 0, 100, 500, 1e9],
    "buy_ratio1h": [0, 0.4, 0.5, 0.6, 0.7, 1.01],
    "tx1h": [0, 50, 200, 1000, 5000, 1e9],
    "buyers1h": [0, 20, 100, 300, 1000, 1e9],
    "socials": [0, 1, 2, 10],
}

GRID = {
    "age_min_h": [0, 1, 24],
    "age_max_h": [6, 72, 336, 1e9],
    "mc_min": [0, 30e3, 100e3],
    "mc_max": [300e3, 1e6, 10e6, 1e12],
    "liq_min": [0, 20e3, 50e3],
    "vol24_min": [0, 50e3, 200e3],
    "wake_min": [0, 2, 5],
    "ch1h_max": [50, 1e9],
    "social_min": [0, 1],
}


def rule_ok(p, r):
    a = p["age_h"]
    return (a is not None and r["age_min_h"] <= a < r["age_max_h"] and r["mc_min"] <= (p["mc"] or 0) < r["mc_max"]
            and (p["liq"] or 0) >= r["liq_min"] and p["vol24h"] >= r["vol24_min"] and p["wake"] >= r["wake_min"]
            and (p["ch1h"] or 0) <= r["ch1h_max"] and p["socials"] >= r["social_min"])


def search(pts, min_n=40, min_tok=15, top=15):
    keys = list(GRID)
    res = []
    for combo in itertools.product(*GRID.values()):
        r = dict(zip(keys, combo))
        if r["age_min_h"] >= r["age_max_h"] or r["mc_min"] >= r["mc_max"]:
            continue
        sel = [p for p in pts if rule_ok(p, r)]
        st = stats(sel)
        if st["n"] >= min_n and st["tokens"] >= min_tok:
            res.append((r, st))
    return sorted(res, key=lambda x: -x[1]["ret"])[:top], sorted(res, key=lambda x: -x[1]["pump"])[:top]


def main(d, horizon_h=24):
    series, meta, lists, boosts = load(d)
    span = [min((s[0]["t"] for s in series.values()), default=0), max((s[-1]["t"] for s in series.values()), default=0)]
    print(f"tokens with snapshots: {len(series)}, snapshots: {sum(map(len, series.values()))}, "
          f"span {(span[1] - span[0]) / HOUR:.1f} h, horizon {horizon_h} h")
    pts = decision_points(series, meta, lists, horizon_h * HOUR)
    print("decision points with a known outcome:", fmt(stats(pts)))
    for ch in sorted({p["chain"] for p in pts}):
        print(f"  {ch:10s}", fmt(stats([p for p in pts if p["chain"] == ch])))
    if len(pts) < 50:
        print("not enough data yet")
        return
    for f, edges in FEATS.items():
        print(f"\n{f}:")
        for lo, hi, st in buckets(pts, f, edges):
            print(f"  [{lo:>10.4g}, {hi:>10.4g})  {fmt(st)}")
    by_ret, by_pump = search(pts)
    print("\nbest filters by average trade return:")
    for r, st in by_ret:
        print("  ", fmt(st), {k: v for k, v in r.items() if v not in (0, 1e9, 1e12)})
    print("\nbest filters by pump rate:")
    for r, st in by_pump:
        print("  ", fmt(st), {k: v for k, v in r.items() if v not in (0, 1e9, 1e12)})
    json.dump(dict(horizon_h=horizon_h, overall=stats(pts),
                   buckets={f: [(lo, hi, st) for lo, hi, st in buckets(pts, f, e)] for f, e in FEATS.items()},
                   best_by_ret=by_ret, best_by_pump=by_pump),
              open(os.path.join(d, f"learn_{horizon_h}h.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 24)
