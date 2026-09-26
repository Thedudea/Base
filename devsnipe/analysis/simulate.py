"""Replay simulator for a GMGN "Dev Snipe" position on pump.fun tokens.

For each historical token we insert our own buy into the real, ordered trade
tape at the slot where a GMGN dev-snipe transaction would plausibly land, then
replay every later trade against the bonding curve *as modified by our
position* (buyers pay by SOL or token amount as they did on-chain, sellers sell
the same token amounts). TP/SL triggers fire on the replayed price; the sell
lands after a sampled reaction delay, again at a random position inside its
slot, so trades that happened in between (e.g. the dev dumping) hit us first.

Costs modelled per transaction: base fee, priority fee, tip, GMGN fee, pump.fun
protocol+creator fee (read from the on-chain events), token-account rent, and
failed transactions (slippage) that still pay fees.
"""

import math
import random
from dataclasses import dataclass, field

LAMPORTS = 1_000_000_000


@dataclass
class Costs:
    priority_fee_sol: float = 0.00045  # GMGN preset "priority fee"
    tip_sol: float = 0.001  # GMGN preset "tip" (anti-MEV / Jito-style)
    base_fee_lamports: int = 5000
    gmgn_fee_rate: float = 0.01  # GMGN platform fee per trade
    ata_rent_sol: float = 0.00207408  # Token-2022 ATA rent (refundable only if the account is closed)
    rent_refunded: bool = False
    failed_tx_pays_tip: bool = False  # bundles that fail usually do not land -> no tip


@dataclass
class Latency:
    """Slot-offset distributions (1 slot ~= 0.4 s)."""

    entry: dict  # slots after the create slot -> probability (0 = same block, after the create tx)
    reaction: dict  # slots from the trigger trade's slot to the sell landing
    name: str = ""

    def draw_entry(self, rng):
        return _draw(self.entry, rng)

    def draw_reaction(self, rng):
        return _draw(self.reaction, rng)


def _draw(dist, rng):
    r = rng.random()
    acc = 0.0
    for k, p in dist.items():
        acc += p
        if r <= acc:
            return k
    return list(dist)[-1]


LATENCY_PRESETS = {
    # entry: GMGN dev snipe reacting to the create tx; reaction: TP/SL engine + sell landing
    "optimistic": Latency(
        entry={0: 0.20, 1: 0.45, 2: 0.25, 3: 0.10},
        reaction={1: 0.15, 2: 0.35, 3: 0.30, 4: 0.20},
        name="optimistic",
    ),
    "typical": Latency(
        entry={1: 0.30, 2: 0.35, 3: 0.20, 4: 0.10, 6: 0.05},
        reaction={2: 0.15, 3: 0.25, 4: 0.25, 5: 0.15, 7: 0.10, 10: 0.10},
        name="typical",
    ),
    "slow": Latency(
        entry={2: 0.20, 3: 0.30, 4: 0.25, 6: 0.15, 8: 0.10},
        reaction={3: 0.15, 4: 0.25, 5: 0.25, 7: 0.20, 10: 0.15},
        name="slow",
    ),
}


def fixed_latency(entry_slot, reaction_slots):
    return Latency(entry={entry_slot: 1.0}, reaction={reaction_slots: 1.0}, name=f"L{entry_slot}/R{reaction_slots}")


@dataclass
class Strategy:
    amount_sol: float = 0.1
    # (trigger_pct, sell_pct_of_remaining); trigger_pct > 0 is TP, < 0 is SL
    levels: list = field(default_factory=lambda: [(15, 100), (-30, 100)])
    buy_slippage_pct: float = 30.0
    sell_slippage_pct: float = 50.0
    sell_retries: int = 3
    exit_on_dev_sell: bool = False  # sell everything when the dev's first sell is seen
    max_hold_slots: int = None  # optional time stop
    trigger_basis: str = "cost"  # "cost": vs SOL spent per token (GMGN buy price); "curve": vs curve price
    slip_ref: str = "create"  # buy slippage measured vs the curve after the create tx, or after the dev's bundle


class Curve:
    __slots__ = ("vsr", "vtr")

    def __init__(self, vsr, vtr):
        self.vsr, self.vtr = vsr, vtr

    @property
    def price(self):
        return self.vsr / self.vtr

    def quote_buy_sol(self, sol_in):
        return sol_in * self.vtr // (self.vsr + sol_in)

    def buy_sol(self, sol_in):
        tok = self.quote_buy_sol(sol_in)
        self.vsr += sol_in
        self.vtr -= tok
        return tok

    def buy_tok(self, tok):
        tok = min(tok, self.vtr - 1)
        sol = -(-tok * self.vsr // (self.vtr - tok))
        self.vsr += sol
        self.vtr -= tok
        return sol

    def quote_sell_tok(self, tok):
        return tok * self.vsr // (self.vtr + tok)

    def sell_tok(self, tok):
        sol = self.quote_sell_tok(tok)
        self.vsr -= sol
        self.vtr += tok
        return sol


def _slot_insert_index(trades, slot, rng, after_idx=-1):
    """Index at which a tx landing in `slot` is inserted (uniform position within the slot)."""
    lo = None
    hi = None
    for i in range(after_idx + 1, len(trades)):
        s = trades[i]["slot"]
        if s >= slot and lo is None:
            lo = i
        if s > slot:
            hi = i
            break
    if lo is None:
        return len(trades)
    if hi is None:
        hi = len(trades)
    if trades[lo]["slot"] > slot:
        return lo
    return rng.randint(lo, hi)  # anywhere among the trades of that slot


def simulate_token(tok, strat: Strategy, lat: Latency, costs: Costs, rng: random.Random, dev=None):
    trades = tok["trades"]
    if not trades:
        return None
    create_slot = tok["creation"]["slot"]
    fee_rate = (trades[0].get("fee_bps", 95) + trades[0].get("cfee_bps", 30)) / 10_000
    prio = int(costs.priority_fee_sol * LAMPORTS)
    tip = int(costs.tip_sol * LAMPORTS)
    per_tx = costs.base_fee_lamports + prio + tip
    failed_tx = costs.base_fee_lamports + prio + (tip if costs.failed_tx_pays_tip else 0)
    rent = int(costs.ata_rent_sol * LAMPORTS)

    # ---- entry: the create tx and the dev's bundle (consecutive tx indices right
    # after it, e.g. the dev's own snipers) always precede us
    L = lat.draw_entry(rng)
    entry_slot = create_slot + L
    n_create = sum(1 for t in trades if t.get("in_create_tx"))
    n_bundle = bundle_len(trades, create_slot)
    idx = max(_slot_insert_index(trades, entry_slot, rng, after_idx=n_bundle - 1), n_bundle)
    if idx >= len(trades):
        return dict(result="no_trades_after_entry", pnl_sol=0.0, L=L)
    curve = Curve(trades[idx]["pre_vsr"], trades[idx]["pre_vtr"])
    # buy slippage is measured against the price GMGN saw right after the create tx
    n_ref = n_bundle if strat.slip_ref == "bundle" else n_create
    ref = trades[n_ref - 1] if n_ref else trades[0]
    ref_curve = Curve(ref["vsr"], ref["vtr"])

    amount = int(strat.amount_sol * LAMPORTS)
    to_curve_total = amount - int(amount * costs.gmgn_fee_rate)
    sol_in = int(to_curve_total / (1 + fee_rate))
    expected_tok = ref_curve.quote_buy_sol(sol_in)
    if curve.quote_buy_sol(sol_in) < expected_tok * (1 - strat.buy_slippage_pct / 100):
        return dict(result="buy_failed_slippage", pnl_sol=-failed_tx / LAMPORTS, pnl_pct=-failed_tx / amount * 100, L=L)
    got_tok = curve.buy_sol(sol_in)
    # TP/SL reference: what GMGN shows as your buy price (SOL spent incl. % fees / tokens)
    entry_price = (amount if strat_basis(strat) == "cost" else sol_in) / got_tok
    holding = got_tok
    proceeds = 0
    extra_fees = 0  # failed sells
    n_tx = 1
    fired = set()
    pending = []
    events = [("buy", entry_slot, idx, got_tok)]

    def schedule(after_idx, trigger_slot, frac, reason, tries):
        land = trigger_slot + lat.draw_reaction(rng)
        at = _slot_insert_index(trades, land, rng, after_idx=after_idx)
        pending.append(dict(at=at, land=land, frac=frac, reason=reason, tries=tries, ref=curve.price))

    dev_sold_seen = False
    i = idx
    while True:
        for p in sorted([p for p in pending if p["at"] <= i], key=lambda p: p["at"]):
            pending.remove(p)
            qty = holding if p["frac"] >= 1 else int(holding * p["frac"])
            if qty <= 0:
                continue
            quote = curve.quote_sell_tok(qty)
            if quote < qty * p["ref"] * (1 - strat.sell_slippage_pct / 100):
                extra_fees += failed_tx
                events.append(("sell_failed", p["land"], i, qty, p["reason"]))
                if p["tries"] < strat.sell_retries:
                    schedule(i - 1, p["land"], p["frac"], p["reason"] + "+retry", p["tries"] + 1)
                continue
            sol_out = curve.sell_tok(qty)
            net = sol_out - int(sol_out * fee_rate)
            proceeds += net - int(net * costs.gmgn_fee_rate)
            n_tx += 1
            holding -= qty
            events.append(("sell", p["land"], i, qty, p["reason"], sol_out))
        if i >= len(trades) or (holding <= 0 and not pending):
            break

        t = trades[i]
        if t["is_buy"]:
            if t.get("ix_name") == "buy":
                curve.buy_tok(t["tok"])
            else:
                curve.buy_sol(t["sol"])
        else:
            curve.sell_tok(t["tok"])
        if dev and t["user"] == dev and not t["is_buy"]:
            dev_sold_seen = True

        if holding > 0:
            chg = (curve.price / entry_price - 1) * 100
            for trig, pct in strat.levels:
                if (trig, pct) in fired:
                    continue
                if (trig > 0 and chg >= trig) or (trig < 0 and chg <= trig):
                    fired.add((trig, pct))
                    schedule(i, t["slot"], pct / 100, f"{'TP' if trig > 0 else 'SL'}{trig}", 0)
            if strat.exit_on_dev_sell and dev_sold_seen and "dev" not in fired:
                fired.add("dev")
                schedule(i, t["slot"], 1.0, "dev_sell", 0)
            if strat.max_hold_slots is not None and "time" not in fired and t["slot"] - entry_slot >= strat.max_hold_slots:
                fired.add("time")
                schedule(i, t["slot"], 1.0, "time", 0)
        i += 1

    # anything still held is marked at the final replayed price, net of exit costs
    mark = 0
    if holding > 0:
        q = curve.quote_sell_tok(holding)
        q -= int(q * fee_rate)
        mark = max(q - int(q * costs.gmgn_fee_rate) - per_tx, 0)
    refund = rent if costs.rent_refunded else 0
    spent = amount + n_tx * per_tx + extra_fees + rent
    pnl = proceeds + mark + refund - spent
    sells = [e for e in events if e[0] == "sell"]
    return dict(
        result="ok",
        L=L,
        entry_idx=idx,
        entry_slot=entry_slot,
        tokens=got_tok,
        spent_sol=spent / LAMPORTS,
        proceeds_sol=(proceeds + mark + refund) / LAMPORTS,
        pnl_sol=pnl / LAMPORTS,
        pnl_pct=pnl / amount * 100,
        still_holding=holding > 0,
        exit=sells[0][4] if sells else ("held" if holding > 0 else "none"),
        exit_slot_offset=(sells[0][1] - create_slot) if sells else None,
        events=events,
    )


def bundle_len(trades, create_slot):
    """Number of leading trades that sit in consecutive tx indices from the create tx."""
    n = 0
    prev = None
    for t in trades:
        if t["slot"] != create_slot:
            break
        ti = t.get("tx_index")
        if n and (ti is None or prev is None or ti - prev > 1):
            break
        prev = ti
        n += 1
    return max(n, sum(1 for t in trades if t.get("in_create_tx")))


def strat_basis(strat):
    return getattr(strat, "trigger_basis", "cost")


def run_many(tokens, strat, lat, costs, n_draws=200, seed=7, dev=None):
    rng = random.Random(seed)
    rows = []
    for tok in tokens:
        for k in range(n_draws):
            r = simulate_token(tok, strat, lat, costs, rng, dev=dev)
            if r is None:
                continue
            r["mint"] = tok["mint"]
            r.pop("events", None)
            rows.append(r)
    return rows


def summarize(rows, amount_sol):
    if not rows:
        return {}
    pnl = sorted(r["pnl_sol"] for r in rows)
    n = len(pnl)
    wins = sum(1 for p in pnl if p > 0)
    ok = [r for r in rows if r["result"] == "ok"]
    exits = {}
    for r in ok:
        exits[r["exit"]] = exits.get(r["exit"], 0) + 1
    return dict(
        n=n,
        mean_pnl_sol=sum(pnl) / n,
        mean_pnl_pct=sum(pnl) / n / amount_sol * 100,
        median_pnl_pct=pnl[n // 2] / amount_sol * 100,
        p10_pct=pnl[int(n * 0.10)] / amount_sol * 100,
        p90_pct=pnl[int(n * 0.90)] / amount_sol * 100,
        win_rate=wins / n,
        hit_10pct=sum(1 for p in pnl if p >= 0.10 * amount_sol) / n,
        buy_failed=sum(1 for r in rows if r["result"] == "buy_failed_slippage") / n,
        exits={k: v / max(1, len(ok)) for k, v in sorted(exits.items(), key=lambda x: -x[1])},
    )
