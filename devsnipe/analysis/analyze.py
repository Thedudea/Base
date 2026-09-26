"""Behavioural analysis of a dev wallet's pump.fun launches.

    python analyze.py ../data/v1/<dev>   -> prints tables, writes <dev>/analysis.json
"""

import json
import os
import statistics as st
import sys
from collections import Counter, defaultdict

from parse import PUMP, account_keys, events_from_tx, load_dev, load_dev_txs, system_transfers

SLOT_SEC = 0.4


def mcap_sol(vsr, vtr):
    # price (SOL/token) * 1e9 supply; token has 6 decimals, SOL 9
    return vsr / vtr * 1e6


def dev_flows(dev, dev_txs):
    """SOL sent from / received by the dev outside of pump.fun trades."""
    out, inn = Counter(), Counter()
    for tx in dev_txs:
        if tx["meta"]["err"]:
            continue
        for f, t, lam in system_transfers(tx):
            if f == dev and t != dev:
                out[t] += lam
            elif t == dev and f != dev:
                inn[f] += lam
    return out, inn


def token_profile(tok, dev):
    trades = tok["trades"]
    cs = tok["creation"]["slot"]
    ct = tok["creation"]["blockTime"]
    prof = dict(mint=tok["mint"], create_slot=cs, create_time=ct, n_trades=len(trades), total_sigs=tok["total_sigs"],
                failed_sigs=tok["failed_sigs"], truncated=tok["truncated"])
    if not trades:
        return prof
    dev_buys = [t for t in trades if t["user"] == dev and t["is_buy"]]
    dev_sells = [t for t in trades if t["user"] == dev and not t["is_buy"]]
    fee_rate = (trades[0]["fee_bps"] + trades[0]["cfee_bps"]) / 1e4
    prof["dev_buy_sol"] = sum(t["sol"] * (1 + fee_rate) for t in dev_buys) / 1e9
    prof["dev_sell_sol"] = sum(t["sol"] * (1 - fee_rate) for t in dev_sells) / 1e9
    prof["dev_pnl_sol"] = prof["dev_sell_sol"] - prof["dev_buy_sol"]
    prof["dev_first_sell_off"] = (dev_sells[0]["slot"] - cs) if dev_sells else None
    prof["dev_last_sell_off"] = (dev_sells[-1]["slot"] - cs) if dev_sells else None
    prof["dev_first_sell_seq"] = dev_sells[0]["seq"] if dev_sells else None
    prof["dev_n_sells"] = len(dev_sells)
    prof["dev_sold_all"] = sum(t["tok"] for t in dev_sells) >= 0.99 * sum(t["tok"] for t in dev_buys) if dev_buys else None

    # price path (post-trade mcap in SOL) by slot offset
    path = defaultdict(list)
    for t in trades:
        path[t["slot"] - cs].append(mcap_sol(t["vsr"], t["vtr"]))
    after_create = [t for t in trades if not t["in_create_tx"]]
    create_mcap = mcap_sol(trades[0]["vsr"], trades[0]["vtr"]) if trades[0]["in_create_tx"] else None
    n_create = sum(1 for t in trades if t["in_create_tx"])
    if n_create:
        create_mcap = mcap_sol(trades[n_create - 1]["vsr"], trades[n_create - 1]["vtr"])
    prof["mcap_after_create"] = create_mcap
    peak = max(trades, key=lambda t: t["vsr"] / t["vtr"])
    prof["peak_mcap"] = mcap_sol(peak["vsr"], peak["vtr"])
    prof["peak_off"] = peak["slot"] - cs
    prof["peak_before_dev_sell"] = (peak["seq"] < prof["dev_first_sell_seq"]) if dev_sells else None
    prof["final_mcap"] = mcap_sol(trades[-1]["vsr"], trades[-1]["vtr"])
    prof["last_trade_off"] = trades[-1]["slot"] - cs
    prof["path"] = {k: (min(v), max(v), v[-1]) for k, v in sorted(path.items()) if k <= 60}
    prof["buy_vol_sol"] = sum(t["sol"] for t in trades if t["is_buy"]) / 1e9
    prof["sell_vol_sol"] = sum(t["sol"] for t in trades if not t["is_buy"]) / 1e9
    prof["buy_vol_before_dev_sell"] = (
        sum(t["sol"] for t in trades if t["is_buy"] and t["user"] != dev and dev_sells and t["seq"] < dev_sells[0]["seq"]) / 1e9
    )
    prof["unique_traders"] = len({t["user"] for t in trades})

    # early buyers (first 12 slots)
    early = []
    for t in after_create:
        if t["slot"] - cs > 12:
            break
        if t["is_buy"]:
            info = tok["txinfo"].get(t["sig"], {})
            early.append(
                dict(
                    user=t["user"],
                    off=t["slot"] - cs,
                    seq=t["seq"],
                    sol=t["sol"] / 1e9,
                    mcap_after=mcap_sol(t["vsr"], t["vtr"]),
                    prio=info.get("prio_fee"),
                    jito=info.get("jito_tip"),
                    programs=[p for p in info.get("top_programs", []) if p not in (PUMP,)],
                    transfers=[(to, lam) for _, to, lam in info.get("payer_transfers", [])],
                    same_tx_as_create=t["in_create_tx"],
                )
            )
    prof["early_buyers"] = early
    return prof


def wallet_pnl_on_token(tok, user):
    fee_rate = None
    spent = got = 0
    for t in tok["trades"]:
        if t["user"] != user:
            continue
        fee_rate = (t["fee_bps"] + t["cfee_bps"]) / 1e4
        if t["is_buy"]:
            spent += t["sol"] * (1 + fee_rate)
        else:
            got += t["sol"] * (1 - fee_rate)
    return spent / 1e9, got / 1e9


def analyze(dev_dir):
    dev = os.path.basename(os.path.normpath(dev_dir))
    toks = load_dev(dev_dir)
    dev_txs = load_dev_txs(dev_dir)
    profs = [token_profile(t, dev) for t in toks]

    # --- recurring early buyers ("snipers")
    appear = defaultdict(list)
    for tok, p in zip(toks, profs):
        seen = set()
        for b in p.get("early_buyers", []):
            if b["user"] in seen or b["user"] == dev:
                continue
            seen.add(b["user"])
            appear[b["user"]].append((tok, b))
    n_tok = max(1, len(toks))
    snipers = []
    for user, rows in appear.items():
        if len(rows) < max(3, 0.2 * n_tok):
            continue
        pnl = [wallet_pnl_on_token(tok, user) for tok, _ in rows]
        sell_offs = []
        for tok, _ in rows:
            ss = [t for t in tok["trades"] if t["user"] == user and not t["is_buy"]]
            if ss:
                sell_offs.append(ss[0]["slot"] - tok["creation"]["slot"])
        snipers.append(
            dict(
                wallet=user,
                tokens=len(rows),
                share=len(rows) / n_tok,
                median_off=st.median(b["off"] for _, b in rows),
                offs=Counter(b["off"] for _, b in rows),
                in_create_tx=sum(1 for _, b in rows if b["same_tx_as_create"]),
                median_buy_sol=st.median(b["sol"] for _, b in rows),
                total_spent=sum(s for s, _ in pnl),
                total_got=sum(g for _, g in pnl),
                median_first_sell_off=st.median(sell_offs) if sell_offs else None,
                programs=Counter(p for _, b in rows for p in b["programs"]).most_common(3),
            )
        )
    snipers.sort(key=lambda s: -s["tokens"])

    out_flow, in_flow = dev_flows(dev, dev_txs)
    for s in snipers:
        s["funded_by_dev_sol"] = out_flow.get(s["wallet"], 0) / 1e9
        s["sent_to_dev_sol"] = in_flow.get(s["wallet"], 0) / 1e9

    res = dict(dev=dev, n_tokens=len(toks), tokens=profs, snipers=snipers,
               dev_top_out=[(k, v / 1e9) for k, v in out_flow.most_common(15)],
               dev_top_in=[(k, v / 1e9) for k, v in in_flow.most_common(15)])
    return res, toks


def _fmt(x, nd=2):
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def print_report(res):
    profs = [p for p in res["tokens"] if p.get("n_trades")]
    print(f"\n=== dev {res['dev']}  tokens analysed: {res['n_tokens']}")
    if not profs:
        return
    times = sorted(p["create_time"] for p in profs)
    gaps = [b - a for a, b in zip(times, times[1:])]
    if gaps:
        print(f"launch interval: median {st.median(gaps)/60:.1f} min, span {(times[-1]-times[0])/3600:.1f} h")
    keys = ["dev_buy_sol", "dev_pnl_sol", "dev_first_sell_off", "dev_last_sell_off", "mcap_after_create", "peak_mcap",
            "peak_off", "final_mcap", "buy_vol_sol", "buy_vol_before_dev_sell", "n_trades", "unique_traders"]
    for k in keys:
        vals = [p[k] for p in profs if p.get(k) is not None]
        if vals:
            print(f"  {k:26s} median {_fmt(st.median(vals))}  min {_fmt(min(vals))}  max {_fmt(max(vals))}")
    pk = [p["peak_before_dev_sell"] for p in profs if p.get("peak_before_dev_sell") is not None]
    if pk:
        print(f"  peak happens before dev's first sell: {sum(pk)}/{len(pk)}")
    print("\n  recurring early buyers:")
    for s in res["snipers"][:12]:
        print(f"   {s['wallet']} tokens {s['tokens']} ({s['share']*100:.0f}%) off~{s['median_off']} "
              f"inCreateTx {s['in_create_tx']} buy~{s['median_buy_sol']:.3f} pnl {s['total_got']-s['total_spent']:+.3f} "
              f"sell@{_fmt(s['median_first_sell_off'])} fundedByDev {s['funded_by_dev_sol']:.3f} progs {s['programs']}")


if __name__ == "__main__":
    d = sys.argv[1]
    res, _ = analyze(d)
    print_report(res)
    json.dump(res, open(os.path.join(d, "analysis.json"), "w"), default=str, indent=1)
