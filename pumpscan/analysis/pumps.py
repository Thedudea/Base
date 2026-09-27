"""Find the big moves of each token and describe what it looked like just before.

    python pumps.py ../data/v1-examples

For every token: merge the candles of all its pools, turn price into market cap,
find the largest run-ups (low -> later high), and measure the pre-pump state:
age, market cap, distance from the previous ATH, volume before vs. baseline,
how long the move took, and how much was left if you bought after the first 2x.
"""

import glob
import json
import os
import sys
from datetime import datetime, timezone

TF_SEC = {"m1": 60, "m5": 300, "hour": 3600, "day": 86400}


def load(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


def ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_usd(v):
    if v is None:
        return "?"
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= d:
            return f"${v / d:.1f}{s}"
    return f"${v:.0f}"


def pct(v):
    return "?" if v is None else f"{v:.0%}"


def xs(v):
    return "?" if v is None else f"{v:.1f}x"


def dur(sec):
    if sec < 3600:
        return f"{sec / 60:.0f}m"
    if sec < 86400 * 2:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


def merged_series(tok_dir):
    """{tf: [(ts, o, h, l, c, vol)]} merged over pools: volume summed, prices from the busiest pool."""
    out = {}
    pools = []
    for f in glob.glob(os.path.join(tok_dir, "ohlcv_*.json")):
        d = load(f)
        if d:
            pools.append(d)
    for tf in TF_SEC:
        by = {}
        for d in pools:
            for c in d["candles"].get(tf, []):
                t, o, h, l, cl, v = c[0], *map(float, c[1:6])
                if t not in by:
                    by[t] = [t, o, h, l, cl, v, v]
                else:
                    cur = by[t]
                    if v > cur[6]:
                        cur[1:5] = [o, h, l, cl]
                        cur[6] = v
                    cur[5] += v
        out[tf] = [tuple(x[:6]) for x in sorted(by.values())]
    return out, pools


def supply_of(tok_dir):
    for f in glob.glob(os.path.join(tok_dir, "gt_token_*.json")):
        a = (load(f) or {}).get("data", {}).get("attributes", {})
        s = a.get("normalized_total_supply")
        if s:
            return float(s)
        if a.get("fdv_usd") and a.get("price_usd"):
            return float(a["fdv_usd"]) / float(a["price_usd"])
    ds = load(os.path.join(tok_dir, "dexscreener.json")) or {}
    for p in ds.get("pairs") or []:
        if p.get("fdv") and p.get("priceUsd"):
            return float(p["fdv"]) / float(p["priceUsd"])
    return None


def run_ups(series, min_ratio=3.0, k=3):
    """Largest non-overlapping low->high moves, greedily (biggest first)."""
    found = []
    blocked = []
    n = len(series)
    for _ in range(k):
        best = None
        lo_i = None
        for j in range(n):
            if any(a <= j <= b for a, b in blocked):
                lo_i = None
                continue
            if lo_i is None or series[j][3] < series[lo_i][3]:
                lo_i = j
            if series[lo_i][3] > 0:
                r = series[j][2] / series[lo_i][3]
                if best is None or r > best[0]:
                    best = (r, lo_i, j)
        if not best or best[0] < min_ratio:
            break
        found.append(best)
        blocked.append((best[1], best[2]))
    return sorted(found, key=lambda x: series[x[1]][0])


def describe(series, i, j, supply, created, tf):
    step = TF_SEC[tf]
    t0, t1 = series[i][0], series[j][0]
    low, high = series[i][3], series[j][2]
    prior = series[:i]
    ath_before = max((c[2] for c in prior), default=None)
    vol = lambda a, b: sum(c[5] for c in series if a <= c[0] < b)
    v24_before = vol(t0 - 86400, t0)
    v7d_base = vol(t0 - 8 * 86400, t0 - 86400) / 7 if t0 - 8 * 86400 >= created else None
    v6h_before = vol(t0 - 6 * 3600, t0)
    # first candle where price closed at 2x the low: what was left from there?
    k2 = next((m for m in range(i, j + 1) if series[m][4] >= 2 * low), None)
    after2x = high / series[k2][4] if k2 is not None else None
    # quiet period: candles in the 24h before the low whose range stayed within +-30% of the low
    quiet = [c for c in prior if c[0] >= t0 - 86400]
    flat = max((c[2] for c in quiet), default=low) / low if quiet else None
    # how far it fell after the peak (next 24h)
    post = [c for c in series[j + 1:] if c[0] <= t1 + 86400]
    after_low = min((c[3] for c in post), default=None)
    mc = (lambda p: p * supply if supply else None)
    return dict(
        start=ts(t0), age_at_start=dur(max(0, t0 - created)) if created else "?", age_sec=(t0 - created) if created else None,
        mc_start=mc(low), mc_peak=mc(high), x=high / low, took=dur(t1 - t0 + step),
        ath_before_mc=mc(ath_before), off_ath=(1 - low / ath_before) if ath_before else None,
        vol24_before=v24_before, vol6h_before=v6h_before, vol_base_per_day=v7d_base,
        range24_before=flat, left_after_2x=after2x,
        mc_2x=mc(series[k2][4]) if k2 is not None else None,
        drop_24h_after=(1 - after_low / high) if after_low else None,
    )


def token_report(tok_dir):
    addr = os.path.basename(tok_dir)
    meta = load(os.path.join(tok_dir, "meta.json")) or {}
    series, pools = merged_series(tok_dir)
    supply = supply_of(tok_dir)
    created = min((int(datetime.fromisoformat(p["pool"]["attributes"]["pool_created_at"].replace("Z", "+00:00")).timestamp())
                   for p in pools if p["pool"]["attributes"].get("pool_created_at")), default=None)
    pf = load(os.path.join(tok_dir, "pumpfun.json")) or {}
    if pf.get("created_timestamp"):
        created = min(created or 1e18, pf["created_timestamp"] // 1000)
    name = pf.get("name") or pf.get("symbol")
    for p in pools:
        name = name or p["pool"]["attributes"].get("name")
    ds = (load(os.path.join(tok_dir, "dexscreener.json")) or {}).get("pairs") or []
    top = max(ds, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if ds else {}
    info = dict(addr=addr, name=name, nets=meta.get("nets"), created=ts(created) if created else "?", supply=supply,
                pools=[(p["pool"]["attributes"]["name"], (p["pool"]["relationships"].get("dex") or {}).get("data", {}).get("id"),
                        p["pool"]["attributes"].get("pool_created_at")) for p in pools],
                now_mc=top.get("marketCap") or top.get("fdv"), now_liq=(top.get("liquidity") or {}).get("usd"),
                vol24=(top.get("volume") or {}).get("h24"), socials=[s.get("type") for s in (top.get("info") or {}).get("socials", [])],
                websites=len((top.get("info") or {}).get("websites", [])), boosts=(top.get("boosts") or {}).get("active"),
                candles={k: len(v) for k, v in series.items()})
    moves = {}
    for tf in ("hour", "m5"):
        s = series[tf]
        if len(s) < 10:
            continue
        moves[tf] = [describe(s, i, j, supply, created, tf) for _, i, j in run_ups(s)]
    return info, moves, series


def main(d):
    allm = []
    for tok_dir in sorted(glob.glob(os.path.join(d, "*/"))):
        info, moves, _ = token_report(tok_dir.rstrip("/"))
        print(f"\n=== {info['name']}  {info['addr']}  {info['nets']}")
        print(f"  created {info['created']}  now MC {fmt_usd(info['now_mc'])} liq {fmt_usd(info['now_liq'])} vol24 {fmt_usd(info['vol24'])}"
              f"  socials {info['socials']} web {info['websites']} boosts {info['boosts']}  candles {info['candles']}")
        for p in info["pools"]:
            print("  pool", p)
        for tf, ms in moves.items():
            for m in ms:
                allm.append((info["name"], tf, m))
                print(f"  [{tf:4s}] {m['start']} age {m['age_at_start']:>6}  MC {fmt_usd(m['mc_start'])} -> {fmt_usd(m['mc_peak'])} "
                      f"({m['x']:.1f}x in {m['took']})  offATH {pct(m['off_ath'])}"
                      f"  vol24b {fmt_usd(m['vol24_before'])} vol6hb {fmt_usd(m['vol6h_before'])} base/d {fmt_usd(m['vol_base_per_day'])}"
                      f"  range24b {xs(m['range24_before'])}  after-2x left {xs(m['left_after_2x'])} (MC {fmt_usd(m['mc_2x'])})"
                      f"  drop24h {pct(m['drop_24h_after'])}")
    json.dump(allm, open(os.path.join(d, "pumps.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main(sys.argv[1])
