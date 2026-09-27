"""Deep study of one dev wallet's launch operation (platform-agnostic).

PnL is measured as each fee payer's real SOL balance change (native + WSOL) in
every transaction that touches a token, so swaps, pump.fun/LaunchLab fees,
priority fees, tips, tool fees and rent are all included.

    python deep_dev.py ../data/<runTag>/<dev>      -> prints report, writes deep.json
"""

import collections
import datetime
import glob
import gzip
import json
import os
import re
import statistics as st
import sys

from parse import PUMP, account_keys, events_from_tx, system_transfers

LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WSOL = "So11111111111111111111111111111111111111112"


def load_gz(p):
    with gzip.open(p, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def sol_delta(tx, who):
    """Native + WSOL balance change of `who` in lamports."""
    keys = account_keys(tx)
    d = 0
    if who in keys:
        i = keys.index(who)
        d += tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in tx["meta"].get(side) or []:
            if b["mint"] == WSOL and b.get("owner") == who:
                d += sign * int(b["uiTokenAmount"]["amount"])
    return d


def token_delta(tx, who, mint):
    d = 0
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in tx["meta"].get(side) or []:
            if b["mint"] == mint and b.get("owner") == who:
                d += sign * int(b["uiTokenAmount"]["amount"])
    return d


def quote_prices(dev, dev_txs, created):
    """SOL per unit for non-SOL quote tokens, from the dev's own pure swaps (median)."""
    dec, rates = {}, collections.defaultdict(list)
    for t in dev_txs:
        for b in (t["meta"].get("postTokenBalances") or []) + (t["meta"].get("preTokenBalances") or []):
            if b.get("owner") == dev and b["mint"] not in created and b["mint"] != WSOL:
                dec[b["mint"]] = b["uiTokenAmount"]["decimals"]
    for t in dev_txs:
        s = sol_delta(t, dev)
        if abs(s) < 20_000_000:  # ignore fee-only SOL moves
            continue
        if any(b["mint"] in created and token_delta(t, dev, b["mint"]) for b in (t["meta"].get("postTokenBalances") or [])):
            continue  # a buy/sell of a launched token, not a pure quote swap
        moved = [m for m in dec if token_delta(t, dev, m)]
        if len(moved) != 1:
            continue
        m = moved[0]
        x = token_delta(t, dev, m)
        if (s > 0) != (x > 0):
            rates[m].append(abs(s) / abs(x))  # lamports per raw unit
    return {m: sorted(r)[len(r) // 2] for m, r in rates.items() if len(r) >= 3}


def onchain_names(dev_txs, created):
    """name/symbol/uri from the create event (borsh strings name, symbol, uri)."""
    import base64
    import struct

    out = {}
    for tx in dev_txs:
        logs = tx["meta"]["logMessages"]
        if not any("InitializeMint" in l for l in logs):
            continue
        mint = next((b["mint"] for b in tx["meta"].get("postTokenBalances") or [] if b["mint"] in created), None)
        if not mint:
            continue
        blobs = [base64.b64decode(l[14:]) for l in logs if l.startswith("Program data: ")]
        keys = account_keys(tx)
        for g in tx["meta"].get("innerInstructions") or []:
            for ix in g["instructions"]:
                if keys[ix["programIdIndex"]] in (PUMP, LAUNCHLAB):
                    try:
                        from parse import b58decode

                        blobs.append(b58decode(ix["data"]))
                    except Exception:
                        pass
        for b in blobs:
            i = b.find(b"http")
            if i < 4:
                continue
            strs, o = [], 0
            while o + 4 <= len(b) and len(strs) < 40:
                n = struct.unpack_from("<I", b, o)[0]
                if 0 < n < 300 and o + 4 + n <= len(b):
                    txt = b[o + 4 : o + 4 + n]
                    try:
                        u = txt.decode("utf-8")
                        if u.isprintable():
                            strs.append((o, u))
                            o += 4 + n
                            continue
                    except UnicodeDecodeError:
                        pass
                o += 1
            for k, (_, u) in enumerate(strs):
                if u.startswith("http") and k >= 2:
                    out[mint] = dict(name=strs[k - 2][1], symbol=strs[k - 1][1], uri=u)
                    break
            if mint in out:
                break
    return out


def tweet_time(url):
    m = re.search(r"/status/(\d+)", url or "")
    return ((int(m.group(1)) >> 22) + 1288834974657) / 1000 if m else None


def main(d, extra_dir=None):
    dev = os.path.basename(os.path.normpath(d))
    dev_txs = [t for t in load_gz(os.path.join(d, "dev_txs.jsonl.gz")) if t["meta"] and not t["meta"]["err"]]
    # optional full histories of other wallets (the dev's bait wallets), indexed by mint
    extra_by_mint = collections.defaultdict(list)
    if extra_dir:
        for p in glob.glob(os.path.join(extra_dir, "*", "dev_txs.jsonl.gz")):
            for t in load_gz(p):
                if not t["meta"] or t["meta"]["err"]:
                    continue
                for m in {b["mint"] for b in (t["meta"].get("postTokenBalances") or []) + (t["meta"].get("preTokenBalances") or [])}:
                    extra_by_mint[m].append(t)
    creations = {c["mint"]: c for c in json.load(open(os.path.join(d, "creations.json")))}
    meta = {}
    mp = os.path.join(d, "metadata.json")
    if os.path.exists(mp):
        for m in json.load(open(mp)):
            j = m.get("json") or {}
            meta[m["uri"]] = j
    # map mint -> metadata via the URI in its create tx logs
    uri_of = {}
    for tx in dev_txs:
        logs = tx["meta"]["logMessages"]
        if not any("InitializeMint" in l for l in logs):
            continue
        blob = " ".join(logs)
        import base64

        for l in logs:
            if l.startswith("Program data: "):
                s = base64.b64decode(l[14:]).decode("latin1")
                for u in re.findall(r"https?://[\x21-\x7e]+", s):
                    for b in tx["meta"].get("postTokenBalances") or []:
                        if b["mint"] in creations:
                            uri_of.setdefault(b["mint"], u)

    prices = quote_prices(dev, dev_txs, set(creations))
    names = onchain_names(dev_txs, set(creations))
    for m, v in names.items():
        uri_of[m] = v["uri"]

    # the dev's funding graph: wallets it sent >1 SOL to (bait / sibling / treasury)
    sent = collections.Counter()
    recv = collections.Counter()
    for tx in dev_txs:
        for f, t, lam in system_transfers(tx):
            if f == dev and t != dev:
                sent[t] += lam
            elif t == dev and f != dev:
                recv[f] += lam
    funded = {w for w, v in sent.items() if v > 1e9}

    # wallets funded by the dev that ever traded its tokens = its "cluster"
    tokens = []
    trader_wallets = collections.defaultdict(lambda: collections.Counter())
    for sp in sorted(glob.glob(os.path.join(d, "mints", "*.sigs.json"))):
        mint = os.path.basename(sp).split(".")[0]
        tp = sp.replace(".sigs.json", ".txs.jsonl.gz")
        if not os.path.exists(tp):
            continue
        sm = json.load(open(sp))
        c = sm["creation"]
        txs = [t for t in load_gz(tp) if t["meta"] and not t["meta"]["err"]]
        seen = {t["transaction"]["signatures"][0] for t in txs}
        txs += [t for t in dev_txs if t["transaction"]["signatures"][0] not in seen
                and any(b["mint"] == mint for b in (t["meta"].get("postTokenBalances") or []) + (t["meta"].get("preTokenBalances") or []))]
        seen |= {t["transaction"]["signatures"][0] for t in txs}
        for t in extra_by_mint.get(mint, []):
            sg = t["transaction"]["signatures"][0]
            if sg not in seen:
                seen.add(sg)
                txs.append(t)
        txs.sort(key=lambda t: (t["slot"], t.get("transactionIndex") or 0))
        platform = "launchlab" if any(LAUNCHLAB in account_keys(t) for t in txs[:3]) else "pump"
        flows = collections.defaultdict(lambda: [0, 0, None, None, 0, 0])  # sol, tokens, first slot, first side, ntx, spent
        for t in txs:
            payer = account_keys(t)[0]
            td = token_delta(t, payer, mint)
            if td == 0 and payer != dev:
                continue
            f = flows[payer]
            v = sol_delta(t, payer) + sum(token_delta(t, payer, q) * px for q, px in prices.items())
            f[0] += v
            if td > 0:
                f[5] += -v
            f[1] += td
            if f[2] is None:
                f[2] = t["slot"] - c["slot"]
                f[3] = "buy" if td > 0 else "sell"
            f[4] += 1
        # pump.fun curve path (for peak / buyer timing)
        evs = []
        if platform == "pump":
            for t in txs:
                for e in events_from_tx(t):
                    if e["mint"] == mint:
                        evs.append((t["slot"] - c["slot"], e))
        dev_sells = [s for s, e in evs if e["user"] == dev and not e["is_buy"]]
        # sell-bot reaction: for each outside buy while the dev still holds, the next dev sell
        reactions = []
        last_dev_sell = dev_sells[-1] if dev_sells else None
        for k, (s0, e) in enumerate(evs):
            if not e["is_buy"] or e["user"] == dev or last_dev_sell is None or s0 > last_dev_sell:
                continue
            nxt = next(((s1, e1) for s1, e1 in evs[k + 1:] if e1["user"] == dev and not e1["is_buy"]), None)
            if nxt:
                reactions.append((s0, e["sol"] / 1e9, nxt[0] - s0, nxt[1]["sol"] / 1e9))
        peak = max((e["vsr"] / e["vtr"] * 1e6 for _, e in evs), default=None)
        uri = uri_of.get(mint)
        j = meta.get(uri, {}) if uri else {}
        tw = j.get("twitter") or ""
        tt = tweet_time(tw)
        dev_buy = flows[dev][5] / 1e9 if dev in flows else 0
        tok = dict(
            mint=mint,
            time=c["blockTime"],
            platform=platform,
            name=j.get("name") or names.get(mint, {}).get("name"),
            symbol=j.get("symbol") or names.get(mint, {}).get("symbol"),
            metadata_live=bool(j),
            twitter=tw,
            author=(tw.split("/")[3] if tw.count("/") >= 3 else None),
            tweet_lag=(c["blockTime"] - tt) if tt else None,
            usepaid="UsePaid" in (j.get("description") or ""),
            image_host=(re.sub(r"^https?://([^/]+)/.*", r"\1", j.get("image") or "") or None),
            n_txs=len(txs),
            total_sigs=sm["totalSigs"],
            window_slots=sm.get("windowSlots"),
            dev_sol=flows[dev][0] / 1e9 if dev in flows else 0.0,
            dev_buy=dev_buy,
            dev_first_sell=dev_sells[0] if dev_sells else None,
            dev_last_sell=dev_sells[-1] if dev_sells else None,
            dev_n_sells=len(dev_sells),
            peak_mcap=peak,
            reactions=reactions,
            tool_fee=sum(l for tx in dev_txs if tx["slot"] == c["slot"] for f_, t_, l in system_transfers(tx) if f_ == dev and t_.startswith("Di45L")) / 1e9,
            wallets={w: dict(sol=f[0] / 1e9, spent=f[5] / 1e9, tok=f[1], first=f[2], side=f[3], ntx=f[4]) for w, f in flows.items()},
        )
        tokens.append(tok)
        for w, f in flows.items():
            trader_wallets[w]["tokens"] += 1

    cluster = {dev} | {w for w in funded if trader_wallets[w]["tokens"] > 0}
    for t in tokens:
        t["cluster_sol"] = sum(v["sol"] for w, v in t["wallets"].items() if w in cluster)
        t["bait_sol_in"] = sum(v["spent"] for w, v in t["wallets"].items() if w in cluster and w != dev)
        outs = {w: v for w, v in t["wallets"].items() if w not in cluster}
        t["outside_in"] = sum(v["spent"] for v in outs.values())
        t["outside_net"] = sum(v["sol"] for v in outs.values())
        t["n_outside"] = len(outs)
        t["outside_losers"] = sum(1 for v in outs.values() if v["sol"] < 0)
        t["slot0_buyers"] = sum(1 for v in outs.values() if v["first"] == 0 and v["side"] == "buy")
    tokens.sort(key=lambda t: t["time"])
    out = dict(dev=dev, quote_prices={k: v * 10 ** 0 for k, v in prices.items()}, cluster=sorted(cluster), funded=sorted(funded),
               sent=[(w, v / 1e9) for w, v in sent.most_common(40)], recv=[(w, v / 1e9) for w, v in recv.most_common(20)],
               tokens=tokens)
    json.dump(out, open(os.path.join(d, "deep.json"), "w"), indent=1, default=str)
    report(out)


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def report(o):
    T = o["tokens"]
    dev = o["dev"]
    n = len(T)
    t0, t1 = T[0]["time"], T[-1]["time"]
    fmt = lambda ts: datetime.datetime.utcfromtimestamp(ts).strftime("%m-%d %H:%M")
    print(f"== {dev}: {n} launches {fmt(t0)} -> {fmt(t1)} UTC ({(t1 - t0) / 3600:.1f} h)")
    print(" platforms", collections.Counter(t["platform"] for t in T))
    gaps = [b["time"] - a["time"] for a, b in zip(T, T[1:])]
    print(f" launch gap median {st.median(gaps) / 60:.1f} min; per hour:",
          sorted(collections.Counter(datetime.datetime.utcfromtimestamp(t['time']).strftime('%d %H') for t in T).items()))
    cs = [t["cluster_sol"] for t in T]
    print(f"\n cluster wallets: {len(o['cluster'])}  {[w[:6] for w in o['cluster']]}")
    print(f" dev-only SOL {sum(t['dev_sol'] for t in T):+.2f}   cluster (dev+funded) SOL {sum(cs):+.2f}")
    print(f" cluster per token: winners {sum(1 for x in cs if x > 0)}/{n}, median {st.median(cs):+.3f}, "
          f"mean {st.mean(cs):+.3f}, top5 {sorted(cs)[-5:]}, worst5 {sorted(cs)[:5]}")
    top = sorted(cs, reverse=True)
    for k in (1, 3, 5, 10):
        print(f"   without top {k:2d}: {sum(top[k:]):+.2f}")
    for pf in ("pump", "launchlab"):
        S = [t for t in T if t["platform"] == pf]
        if S:
            x = [t["cluster_sol"] for t in S]
            print(f" {pf:9s} n={len(S):3d} total {sum(x):+7.2f} winners {sum(1 for v in x if v > 0):3d} median {st.median(x):+.3f}")
    # cumulative
    acc, pk, dd = 0, 0, 0
    for t in T:
        acc += t["cluster_sol"]
        pk = max(pk, acc)
        dd = min(dd, acc - pk)
    print(f" max drawdown along the way {dd:+.2f} SOL")
    # buy size
    print("\n by dev buy size:")
    for lo, hi in ((0, 3), (3, 7), (7, 20), (20, 999)):
        S = [t for t in T if lo <= t["dev_buy"] < hi]
        if S:
            x = [t["cluster_sol"] for t in S]
            print(f"   {lo}-{hi} SOL: n={len(S)} total {sum(x):+.2f} win {sum(1 for v in x if v > 0)}/{len(S)} outside_in median {st.median(t['outside_in'] for t in S):.2f}")
    print("\n bait:")
    for lab, S in (("with bait", [t for t in T if t["bait_sol_in"] > 0]), ("no bait", [t for t in T if t["bait_sol_in"] == 0])):
        if S:
            x = [t["cluster_sol"] for t in S]
            print(f"   {lab:9s} n={len(S)} total {sum(x):+.2f} win {sum(1 for v in x if v > 0)}/{len(S)} outside_in median {st.median(t['outside_in'] for t in S):.2f}")
    print("\n tweet lag (s) vs result:")
    L = [t for t in T if t["tweet_lag"] is not None]
    for lo, hi in ((-5, 5), (5, 10), (10, 20), (20, 60), (60, 1e9)):
        S = [t for t in L if lo <= t["tweet_lag"] < hi]
        if S:
            x = [t["cluster_sol"] for t in S]
            print(f"   {lo}-{hi}: n={len(S)} total {sum(x):+.2f} win {sum(1 for v in x if v > 0)}/{len(S)}")
    print(" UsePaid 'fees to author' vs not:")
    for lab, S in (("usepaid", [t for t in T if t["usepaid"]]), ("plain", [t for t in T if not t["usepaid"]])):
        if S:
            x = [t["cluster_sol"] for t in S]
            print(f"   {lab:8s} n={len(S)} total {sum(x):+.2f} win {sum(1 for v in x if v > 0)}/{len(S)}")
    au = collections.defaultdict(list)
    for t in T:
        au[t["author"]].append(t["cluster_sol"])
    print(" authors used most:", sorted(((a, len(v), round(sum(v), 2)) for a, v in au.items()), key=lambda x: -x[1])[:15])
    R = [r for t in T for r in t.get("reactions", [])]
    if R:
        lat = [r[2] for r in R]
        ratio = [r[3] / r[1] for r in R if r[1] > 0.05]
        print(f"\n sell bot: {len(R)} outside buys answered; next dev sell after median {st.median(lat)} slots "
              f"(same slot {pct(sum(1 for x in lat if x == 0), len(lat))}, <=2 slots {pct(sum(1 for x in lat if x <= 2), len(lat))}); "
              f"dev sell / outside buy median {st.median(ratio):.2f}")
    fs = [t for t in T if t["dev_first_sell"] is not None]
    if fs:
        print(f" dev first sell median +{st.median(t['dev_first_sell'] for t in fs)} slots, last +{st.median(t['dev_last_sell'] for t in fs)}, sells/token {st.median(t['dev_n_sells'] for t in fs)}")
    print(f" tool fee to Di45L… per launch median {st.median(t.get('tool_fee', 0) for t in T):.4f} SOL; metadata still online {sum(1 for t in T if t.get('metadata_live'))}/{n}")
    print("\n outsiders:")
    ow = collections.defaultdict(lambda: [0.0, 0])
    for t in T:
        for w, v in t["wallets"].items():
            if w not in o["cluster"]:
                ow[w][0] += v["sol"]
                ow[w][1] += 1
    vals = [v[0] for v in ow.values()]
    print(f"   wallets {len(ow)}, net {sum(vals):+.2f} SOL, losers {sum(1 for v in vals if v < 0)}, winners {sum(1 for v in vals if v > 0)}")
    rep = sorted(ow.items(), key=lambda kv: -kv[1][1])[:10]
    print("   most frequent:", [(w[:6], n_, round(s, 2)) for w, (s, n_) in rep])
    print("   biggest winners:", [(w[:6], n_, round(s, 2)) for w, (s, n_) in sorted(ow.items(), key=lambda kv: -kv[1][0])[:8]])
    print("   biggest losers:", [(w[:6], n_, round(s, 2)) for w, (s, n_) in sorted(ow.items(), key=lambda kv: kv[1][0])[:8]])
    print("\n money out of the dev (>1 SOL):", [(w[:6], round(v, 1)) for w, v in o["sent"] if v > 1][:20])
    print(" money into the dev:", [(w[:6], round(v, 2)) for w, v in o["recv"] if v > 0.01][:10])
    print("\n top 12 launches:")
    for t in sorted(T, key=lambda t: -t["cluster_sol"])[:12]:
        print(f"   {fmt(t['time'])} {t['platform']:9s} {str(t['name'])[:22]:22s} @{str(t['author'])[:16]:16s} lag {str(round(t['tweet_lag'])) if t['tweet_lag'] is not None else '-':>4}s "
              f"cluster {t['cluster_sol']:+6.2f} outside_in {t['outside_in']:6.2f} n_out {t['n_outside']:3d} peak {t['peak_mcap'] or 0:6.1f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
