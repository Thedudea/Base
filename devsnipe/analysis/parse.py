"""Turn raw getTransaction JSON (fetched by devsnipe/fetch) into ordered
pump.fun trades per token.

Every pump.fun buy/sell emits an Anchor TradeEvent in its logs carrying the
post-trade bonding-curve reserves, so the exact curve state before and after
every trade can be reconstructed and replayed by the simulator.
"""

import base64
import glob
import gzip
import hashlib
import json
import os
import struct
from collections import defaultdict

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"

# Jito tip accounts; other tip services are detected heuristically in analyze.py
JITO_TIPS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKc5wPdSSdeBnizKZ6jT",
}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


TRADE_EVENT = _disc("TradeEvent")
CREATE_EVENT = _disc("CreateEvent")
EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")  # anchor emit_cpi prefix


class _Reader:
    def __init__(self, d: bytes):
        self.d, self.o = d, 0

    def pk(self):
        v = b58encode(self.d[self.o : self.o + 32])
        self.o += 32
        return v

    def u64(self):
        v = struct.unpack_from("<Q", self.d, self.o)[0]
        self.o += 8
        return v

    def i64(self):
        v = struct.unpack_from("<q", self.d, self.o)[0]
        self.o += 8
        return v

    def u8(self):
        v = self.d[self.o]
        self.o += 1
        return v

    def string(self):
        n = struct.unpack_from("<I", self.d, self.o)[0]
        self.o += 4
        v = self.d[self.o : self.o + n].decode(errors="replace")
        self.o += n
        return v

    def left(self):
        return len(self.d) - self.o


def decode_trade_event(d: bytes) -> dict:
    r = _Reader(d)
    ev = dict(
        mint=r.pk(),
        sol=r.u64(),
        tok=r.u64(),
        is_buy=bool(r.u8()),
        user=r.pk(),
        ts=r.i64(),
        vsr=r.u64(),
        vtr=r.u64(),
        rsr=r.u64(),
        rtr=r.u64(),
    )
    if r.left() >= 32 + 8 + 8 + 32 + 8 + 8:
        ev.update(fee_recipient=r.pk(), fee_bps=r.u64(), fee=r.u64(), creator=r.pk(), cfee_bps=r.u64(), cfee=r.u64())
    else:
        ev.update(fee_recipient=None, fee_bps=100, fee=ev["sol"] // 100, creator=None, cfee_bps=0, cfee=0)
    ev["ix_name"] = None
    if r.left() >= 33 + 4:
        r.o += 33  # track_volume, total_unclaimed, total_claimed, current_sol_volume, last_update_ts
        try:
            ev["ix_name"] = r.string()
        except Exception:
            pass
    return ev


def account_keys(tx):
    m = tx["transaction"]["message"]
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return m["accountKeys"] + la.get("writable", []) + la.get("readonly", [])


def events_from_tx(tx):
    """TradeEvents from 'Program data:' logs; falls back to emit_cpi inner ixs."""
    out = []
    for line in tx["meta"].get("logMessages") or []:
        if line.startswith("Program data: "):
            try:
                b = base64.b64decode(line[14:])
            except Exception:
                continue
            if b[:8] == TRADE_EVENT:
                out.append(decode_trade_event(b[8:]))
    if out:
        return out
    keys = account_keys(tx)
    for group in tx["meta"].get("innerInstructions") or []:
        for ix in group["instructions"]:
            if keys[ix["programIdIndex"]] != PUMP:
                continue
            b = b58decode(ix["data"])
            if b[:8] == EVENT_IX_TAG and b[8:16] == TRADE_EVENT:
                out.append(decode_trade_event(b[16:]))
    return out


def system_transfers(tx):
    """(from, to, lamports) for every System Program transfer (top level + inner)."""
    keys = account_keys(tx)
    m = tx["transaction"]["message"]
    ixs = list(m["instructions"])
    for g in tx["meta"].get("innerInstructions") or []:
        ixs.extend(g["instructions"])
    out = []
    for ix in ixs:
        if keys[ix["programIdIndex"]] != SYSTEM:
            continue
        try:
            b = b58decode(ix["data"])
        except Exception:
            continue
        if len(b) >= 12 and struct.unpack_from("<I", b, 0)[0] == 2:
            lam = struct.unpack_from("<Q", b, 4)[0]
            acc = ix["accounts"]
            out.append((keys[acc[0]], keys[acc[1]], lam))
    return out


def compute_budget(tx):
    """(cu_limit, cu_price_microlamports) from ComputeBudget instructions."""
    keys = account_keys(tx)
    limit, price = None, 0
    for ix in tx["transaction"]["message"]["instructions"]:
        if keys[ix["programIdIndex"]] != COMPUTE_BUDGET:
            continue
        b = b58decode(ix["data"])
        if not b:
            continue
        if b[0] == 2 and len(b) >= 5:
            limit = struct.unpack_from("<I", b, 1)[0]
        elif b[0] == 3 and len(b) >= 9:
            price = struct.unpack_from("<Q", b, 1)[0]
    return limit, price


def tx_summary(tx):
    keys = account_keys(tx)
    m = tx["transaction"]["message"]
    nsig = m["header"]["numRequiredSignatures"]
    fee = tx["meta"]["fee"]
    cu_limit, cu_price = compute_budget(tx)
    top_programs = [keys[ix["programIdIndex"]] for ix in m["instructions"]]
    payer = keys[0]
    transfers = [(f, t, l) for f, t, l in system_transfers(tx) if f == payer]
    return dict(
        sig=tx["transaction"]["signatures"][0],
        slot=tx["slot"],
        block_time=tx["blockTime"],
        payer=payer,
        signers=keys[:nsig],
        nsig=nsig,
        tx_fee=fee,
        prio_fee=fee - 5000 * nsig,
        cu_limit=cu_limit,
        cu_price=cu_price,
        cu_used=tx["meta"].get("computeUnitsConsumed"),
        top_programs=[p for p in dict.fromkeys(top_programs) if p not in (COMPUTE_BUDGET,)],
        payer_transfers=transfers,
        jito_tip=sum(l for _, t, l in transfers if t in JITO_TIPS),
        payer_sol_delta=tx["meta"]["postBalances"][0] - tx["meta"]["preBalances"][0],
    )


def pre_state(t):
    """Curve virtual reserves before a trade, from its post-trade event."""
    if t["is_buy"]:
        return t["vsr"] - t["sol"], t["vtr"] + t["tok"]
    return t["vsr"] + t["sol"], t["vtr"] - t["tok"]


def order_trades(trades, sig_rank):
    """Order trades exactly by chaining reserves (pre-state of k == post-state of k-1).

    Falls back to (slot, getSignaturesForAddress rank) where chaining is ambiguous.
    """
    trades.sort(key=lambda t: (t["slot"], sig_rank.get(t["sig"], 0), t["ev_idx"]))
    by_slot = defaultdict(list)
    for t in trades:
        by_slot[t["slot"]].append(t)
    ordered, state = [], None
    chained = 0
    for slot in sorted(by_slot):
        pending = by_slot[slot]
        while pending:
            pick = None
            if state is not None:
                for t in pending:
                    if pre_state(t) == state:
                        pick = t
                        break
            if pick is None:
                pick = pending[0]
            else:
                chained += 1
            pending.remove(pick)
            ordered.append(pick)
            state = (pick["vsr"], pick["vtr"])
    for i, t in enumerate(ordered):
        t["seq"] = i
    return ordered, chained


def parse_mint(sigs_path, txs_path):
    meta = json.load(open(sigs_path))
    creation = meta["creation"]
    mint = creation["mint"]
    # getSignaturesForAddress returns newest first; meta["sigs"] is oldest first
    sig_rank = {s["signature"]: i for i, s in enumerate(meta["sigs"])}
    failed = [s for s in meta["sigs"] if s.get("err")]
    trades = []
    txinfo = {}
    with gzip.open(txs_path, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            tx = json.loads(line)
            if not tx or tx["meta"]["err"]:
                continue
            info = tx_summary(tx)
            evs = [e for e in events_from_tx(tx) if e["mint"] == mint]
            info["n_events"] = len(evs)
            info["is_create"] = info["sig"] == creation["signature"]
            txinfo[info["sig"]] = info
            for k, e in enumerate(evs):
                t = dict(e)
                t.update(
                    sig=info["sig"],
                    slot=info["slot"],
                    block_time=info["block_time"],
                    payer=info["payer"],
                    ev_idx=k,
                    in_create_tx=info["is_create"],
                )
                trades.append(t)
    ordered, chained = order_trades(trades, sig_rank)
    for t in ordered:
        t["pre_vsr"], t["pre_vtr"] = pre_state(t)
    return dict(
        mint=mint,
        creation=creation,
        total_sigs=meta["totalSigs"],
        truncated=meta.get("truncated"),
        failed_sigs=len(failed),
        failed_by_slot=_count_by(failed, "slot"),
        block_idx=meta.get("blockIdx", {}),
        trades=ordered,
        chained=chained,
        txinfo=txinfo,
    )


def _count_by(rows, key):
    c = defaultdict(int)
    for r in rows:
        c[r[key]] += 1
    return dict(c)


def load_dev(dev_dir):
    out = []
    for sp in sorted(glob.glob(os.path.join(dev_dir, "mints", "*.sigs.json"))):
        tp = sp.replace(".sigs.json", ".txs.jsonl.gz")
        if not os.path.exists(tp):
            continue
        out.append(parse_mint(sp, tp))
    out.sort(key=lambda m: m["creation"]["slot"])
    return out


def load_dev_txs(dev_dir):
    p = os.path.join(dev_dir, "dev_txs.jsonl.gz")
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]
