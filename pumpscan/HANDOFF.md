# HANDOFF — continue this project in a new chat / on the user's server

> **For the next assistant:** read this file first. Reply to the user **in Persian, always**.
> All facts below were verified in the previous session. The full history of findings is in
> `pumpscan/REPORT.md` (Persian, sections 1–13) and `devsnipe/REPORT.md`.

---

## 0. خلاصه برای کاربر (فارسی)

**هدف پروژه:** پیدا کردن یک فیلتر GMGN که برآیند معامله‌هایش روی میم‌کوین‌ها مثبت باشد، و بعد خودکار کردن خرید و فروش با همان فیلتر.

**چه کردیم:**
1. هفت توکن پامپ‌کرده را بررسی کردیم.
2. یک اسکنر ساختیم که یک هفته (۲۷ سپتامبر تا ۴ اکتبر ۲۰۲۶) هر ۵ دقیقه بیش از ۲۷ هزار توکن سولانا و Robinhood Chain را ثبت کرد.
3. ده‌ها فیلتر را قفل کردیم و فقط روی داده‌ی بعدی (که ندیده بودند) تست کردیم.

**نتیجه:**
- **فیلتر F4 روی Robinhood Chain برنده شد:** ۶ روز پشت سر هم مثبت، ۲۶۹ معامله، +۱۴۷ واحد، ۶۱٪ سودده.
- **روی سولانا همه‌ی فیلترها ضرر دادند.**
- **خرید فقط به خاطر DEX Paid ضرر می‌دهد**، ولی «F4 + DEX Paid» قوی‌ترین نسخه بود. نمونه‌اش کم است: ۱۷ معامله، ۸۸٪ سودده.

**ربات:** در `pumpscan/bot/` نوشته شده است.
- از API رسمی GMGN استفاده می‌کند.
- **هنوز هیچ‌وقت اجرا نشده.** محیط قبلی اجازه‌ی اجرای تستش را نداد.
- قدم اول روی سرور: اجرا در حالت آزمایشی (`DRY_RUN=1`).

**قدم‌های بعدی:** بخش ۹ همین فایل.

---

## 1. Who the user is / working rules

- Language: **Persian only** in replies (the user got upset when answers came in English).
- Trades meme coins on **GMGN** (gmgn.ai). Accepts per-trade losses if the overall equity curve grows.
- Server: **Ubuntu 24, 2 GB RAM, 1 CPU**. It will now run things directly on it.
- **Security / scam caution:** the user explicitly asked never to use scam sites or links. Only use:
  - gmgn.ai;
  - the npm package `gmgn-cli` (publisher `infra@gmgn.ai`, source github.com/GMGNAI/gmgn-skills, pinned **1.6.6**);
  - api.dexscreener.com, api.geckoterminal.com, api.telegram.org, @BotFather.
  - gmgnaiapp.com is **unofficial** (it was cited once by mistake).
- Earlier constraint (devsnipe phase): the user does **not** want tokens named after real people, or claims that fees go to them. We also declined to build bundled self-sniping wallets.
- The user is fine with keeping a wallet private key on the server for the later "direct wallet" bot (path 3).
- Repo: `Thedudea/Base`, branch **`claude/beautiful-mccarthy-d1pizl`** (PR #1). The user said they made the repo private.
  - Note: GitHub Actions minutes on a private repo are limited, which is one reason to move the scanner to the server.

## 2. Repository map

| path | what |
|---|---|
| `pumpscan/HANDOFF.md` | this file |
| `pumpscan/ROADMAP.md` | **roadmap**: history, current status, step-by-step checklist of what comes next — keep its checkboxes up to date |
| `pumpscan/FILTERS.md` | **every filter** in plain words, its GMGN equivalent, and forward results per chain and per day |
| `pumpscan/REPORT.md` | full Persian report, sections 1–13 (13 = final result) |
| `pumpscan/bot/` | **the trading bot** (`bot.mjs`, `package.json`, `.env.example`, `README-fa.md` = setup guide in Persian) |
| `pumpscan/scan/scanner.mjs` | market scanner (Node, no deps); `config.json` (now `enabled:false`); `run.sh` (GitHub Actions driver) |
| `pumpscan/analysis/explore.py` | core: `Data` class (features per snapshot), trade simulator `sim()`, walk-forward beam search |
| `pumpscan/analysis/forward.py` | scores `frozen_rules.json` only on data after each rule's `frozen_at` |
| `pumpscan/analysis/frozen_rules.json` | all candidate filters (F0–F7, P0–P3, Q2–Q6, G1–G2, H4, H4P, R3, R5) with freeze times — **never edit old rules, add new ones** |
| `pumpscan/analysis/paid.py` | "buy at DEX-paid moment" study |
| `pumpscan/analysis/export_trades.py` | exports per-trade CSV of forward results (all rules by default) |
| `pumpscan/analysis/rule_stats.py` | per-rule/per-chain/per-day tables from that CSV (no raw data needed, low RAM) |
| `pumpscan/analysis/learn.py`, `pumps.py` | early (day-1) analyses; superseded by explore/forward |
| `pumpscan/results/forward_trades.csv` | **every forward-test trade** of all rules F0–H4P (token, time, MC, liq, vol, tx, dex_paid, rug, simulated returns incl. +5 min delay) — use to compare with live bot fills |
| `pumpscan/data/scan/` | raw scanner data, **417 MB** (`YYYY-MM-DD/HHMM.ndjson.gz`, `schema.json`), plus all daily outputs `forward_*.txt`, `explore_*.txt`, `paid_*.txt` |
| `pumpscan/data/v1-examples/` | first study of the 7 pumped tokens |
| `pumpscan/fetch/` | one-off fetcher for the 7 example tokens |
| `devsnipe/` | earlier project: analysis of pump.fun dev wallets (8NJ7…, AufH…, bwamJzzt…); `REPORT.md` Persian; raw data 312 MB |
| `.github/workflows/pumpscan-scan.yml` | scanner on Actions (self-chaining 5.5 h runs; respects `scan/config.json` enabled/until) |
| `.claude/settings.json` | allows git add/commit/push for the assistant |

Getting the code onto the server without the 700 MB of data:
```bash
git clone --filter=blob:none --sparse -b claude/beautiful-mccarthy-d1pizl https://github.com/Thedudea/Base.git
cd Base && git sparse-checkout set pumpscan/bot pumpscan/analysis pumpscan/scan pumpscan/results
# add pumpscan/data/scan later only if the raw data is needed (417 MB)
```
The zip `pumpscan-handoff.zip` (repo root) contains everything except the raw data.

## 3. The winning filter and how it is used

**F4** (frozen 2026-09-28 09:30 UTC):
- liq ≥ $70K
- highest MC seen < $160K (= GMGN "ATH MC" max 160K)
- vol ≥ $70K
- txs < 900

It was tested with 24h windows. **H4** is the same filter on **1h** windows (= GMGN Trending 1h). For tokens this young (median age about 7 min at signal) 1h ≈ 24h.

**GMGN UI settings given to the user** (Trending page, chain **Robinhood**, timeframe **1h**):

| setting | value |
|---|---|
| Launchpads | Select All |
| Metrics checkboxes | only **Exclude Honeypot** on (Exclude Unverified **off**) |
| Liq | Min 70K |
| ATH MC | Max 160K |
| 1h Vol | Min 70K |
| 1h TXs | Max 900 |
| everything else | empty |

**Rules:**
- **Buy:** on a token's **first** appearance, immediately (a 5-min delay lost about 70% of the edge), fixed small size, once per token.
- **Exit (GMGN Advanced TP/SL, Amount mode):**
  - TP +100%, sell 50%;
  - SL −35%, sell 100%;
  - trailing row: TP 100 / DD 35 / sell 50;
  - sell whatever is left after 6 h.
  - The alternative "sell all at 2x, SL −35%" is about 12% worse for F4 and about 2.5× worse for the DEX-paid variants.
- **DEX-paid variant (Q4/H4P):** same filter, buy only if the token has a paid DexScreener profile. Fewer trades (about 4/day), far better per trade; small sample.

## 4. Final forward results (2026-09-28 → 2026-10-04, exit = half at 2x + trail 35% + SL −35%, hold ≤ 6 h, 3% costs, rugs = −100%)

| rule | chain | trades | total (units = stake per trade) | mean | median | win |
|---|---|---|---|---|---|---|
| F0 all tokens | both | 12182 | **−2548** | −21% | −11% | 28% |
| **F4** | Robinhood | 269 | **+147.0** | +55% | +16% | 61% |
| F4 | Solana | 39 | −27.4 | | | |
| F6 | Robinhood | 449 | +138.6 | +31% | −4% | 41% |
| H4 (1h) | Robinhood | 64 (3 days) | +57.7 | +90% | +24% | 59% |
| Q4 F4+DEX paid | Robinhood | 17 | +48.6 | +286% | +222% | 88% |
| Q6 F6+DEX paid | Robinhood | 24 | +69.7 | +291% | +241% | 75% |
| F2 (fast momentum) | Robinhood | 303 | +201.8 | +67% | +19% | 58% |
| F2 | Solana | 117 | −87.0 | | | |
| P0 buy at DEX-paid moment | both | 2160 | −311 | −14% | −46% | 24% |

**Robustness of F4:**
- positive all 6 days;
- max drawdown about −3 units;
- +5 min delay: Robinhood only +64u (vs +147u); both chains +32u (vs +120u), i.e. about 70% of the edge lost;
- 6% costs: still +36% mean (both chains);
- without the top 5 trades: +109u.

**F2:**
- Profitable on Robinhood, but very delay-sensitive: +202u drops to +47u with a 5 min delay.
- With Solana included, it turns negative with the delay.
- It suits a bot only. It was not chosen because the bot was planned later.

Per-trade numbers for all rules: `results/forward_trades.csv`.

**Holders / Top10 data** were collected but did not separate winners from losers (almost all young tokens have top10 ≥ 30% and fewer than 200 holders).

## 5. Lessons / pitfalls (do not repeat)

1. **Survivorship bias:** a preset derived from 7 winners lost money forward. Always freeze rules first, then test only on later data.
2. **Fake / unsellable tokens:**
   - FluxBeam honeypot families (straight-line bot-driven price, about $600 volume) produced fake gains. They are excluded (`BAD_DEX={"fluxbeam"}`, `MIN_VOL24=10K`).
3. **Rugs:**
   - About 25% of tokens had liquidity pulled within 6 h.
   - The price quoted on a drained pool is absurd (up to 1e9×).
   - The simulation counts liquidity < 20% of entry (or < $3K) as −100%.
   - Clone scams like "cat wif sword", "Doppler Finance", "Exxon Mobil Corp".
4. **Pair switching:**
   - Only the top pair per snapshot is stored.
   - Outcomes are measured on the same pair.
   - On graduation (pump.fun curve → PumpSwap) the trade follows the new pool if the price is continuous (1/3×–3×).
5. **pump.fun curve liquidity is null on DexScreener** → estimated as `2*SOL_USD*sqrt(mc/SOL_USD*32.19)`.
6. **Honeypot check on F4 signals:** median max-24h sells per token 330, none with < 10 sells; winners median 457, min 29 → real, sellable.
7. **Speed matters:** most edges vanish with a 5 min entry delay; F4/F6 survive partially.
8. **`explore.py` memory:** with 1.2 M snapshots it was **OOM-killed even on the 15 GB sandbox**. On the 2 GB server do not run it on the full data. Either:
   - restrict to Robinhood / recent days; or
   - rewrite `Data` to stream / compact (float32 arrays, drop Solana).
   - `forward.py` worked in the sandbox but may also need the same treatment on 2 GB.
9. **GeckoTerminal** free API returns 429 at about 1 call / 2.2 s; the scanner uses a 3.5 s gap. GitHub Actions runners were once killed by GitHub (shutdown signal); a watchdog restarted the chain.
10. **GMGN website API (gmgn.ai/defi/...) returns 403** to servers. Use the official `gmgn-cli` / OpenAPI instead.

## 6. Data sources used by the scanner (all free, no keys)

- **GeckoTerminal** `api.geckoterminal.com/api/v2`:
  - `networks/{solana|robinhood}/trending_pools?duration=5m|1h|6h|24h&page=N`
  - `networks/{net}/new_pools`
  - `networks/solana/dexes/pumpswap/pools?sort=h24_tx_count_desc`
  - `networks/{net}/tokens/{addr}/info` → holders count, top10 %, dev %, honeypot flag
  - OHLCV via `networks/{net}/pools/{pool}/ohlcv/{day|hour|minute}`
- **DexScreener** `api.dexscreener.com`:
  - `tokens/v1/{chain}/{up to 30 addrs}` → per-pair price, MC, liq, vol m5/h1/h6/h24, txns, priceChange
  - `token-profiles/latest/v1` = **DEX-paid feed**: about 30 entries, about 2 new per 5 min, so polling every 5 min misses almost nothing
  - `token-boosts/latest|top/v1`
  - `orders/v1/{chain}/{token}` → paid orders (`type: tokenProfile`, `status: approved` = DEX paid); used by the bot
- Robinhood Chain ids:
  - GeckoTerminal network `robinhood`; DexScreener chainId `robinhood`.
  - Common DEX ids: uniswap (v3/v4), pons-v2, bankr.
  - Native gas token ETH. GMGN launchpads on Robinhood: Pons, Pons V2, Long.xyz, Bankr, Trench, Flap, …
- Row schema: `pumpscan/data/scan/schema.json`.
  - Types: `g` = GeckoTerminal list row, `d` = DexScreener snapshot, `m` = meta, `b` = boost/profile event, `h` = holder stats.

**Running the scanner directly on the server** (instead of Actions):
```bash
cd pumpscan/scan && sed -i 's/"enabled": false/"enabled": true/; s/"until": "[^"]*"/"until": "2026-12-31T00:00:00Z"/' config.json
DURATION_MIN=100000 node scanner.mjs ../data/scan-live
```
It is memory-light, but `maxTrackedPerChain` 2500 per chain can be reduced. Set `chains` to `["robinhood"]` to halve the load.

## 7. The bot (`pumpscan/bot/`) — status: written, NEVER executed

**How it works:**
- `gmgn-cli` 1.6.6 is called via `execFile` with `--raw`; output is parsed as JSON.
- Every `POLL_SEC` (60) the bot runs:
  `gmgn-cli market trending --chain robinhood --interval 1h --limit 100 --order-by volume --filter not_honeypot --min-liquidity 70000 --max-history-highest-marketcap 160000 --min-volume 70000 --max-swaps 900`
  - Every condition is re-checked locally on fields `liquidity`, `history_highest_market_cap`, `volume`, `swaps`, `is_honeypot` (from `data.rank[]`).
- **Buy:** first appearance only. Tokens already listed at start are ignored unless `BUY_ON_START=1`.
  - Command: `gmgn-cli swap --chain robinhood --from WALLET --input-token 0x000…000 --output-token ADDR --amount <wei> --slippage 15 --condition-orders <JSON> --sell-ratio-type buy_amount --yes`
  - The CLI requires env `GMGN_ALLOW_AUTOMATED_TRADES=1`, which the bot sets only for trade calls.
- **Condition orders** (`price_scale` for `loss_stop` = drop %):
  `[{profit_stop, price_scale 100, sell_ratio 50}, {profit_stop_trace, price_scale 100, sell_ratio 50, drawdown_rate 35}, {loss_stop, price_scale 35, sell_ratio 100}]`
- **Order status:** polled with `order get` (pending → processed → confirmed | failed | expired).
- **Time exit after `HOLD_HOURS`:** `portfolio token-balance`, then `swap --percent 100` back to ETH.
- **Safety:**
  - `DRY_RUN=1` by default;
  - `MAX_BUYS_PER_DAY`, `MAX_OPEN_POSITIONS`;
  - optional `REQUIRE_DEX_PAID=1` (DexScreener orders endpoint).
- **Telegram:** notifications plus `/status /pause /resume`, accepted only from `TELEGRAM_CHAT_ID`.
- **Files:**
  - state in `state.json`;
  - log in `trades.csv`;
  - `.env`, `state.json`, `trades.csv`, `node_modules` are git-ignored.
- **Auth:**
  - `npx gmgn-cli config` creates an Ed25519 request-signing key pair locally and prints a gmgn.ai link to create the API key.
  - `npx gmgn-cli config --apply <KEY>` stores both in `~/.config/gmgn/.env`.
  - The signing key is **not** a wallet key.
  - The wallet address comes from `npx gmgn-cli portfolio info`.
  - API key is free; the free plan has rate limits (leaky bucket 5, trending weight 3); every trade pays GMGN's 1% fee.

**Unverified assumptions — check against real output first (in DRY_RUN and with the first tiny live trade):**
1. JSON paths: `data.rank[]` and field names; swap response `data.order_id`; `order get` → `data.status` values; `portfolio token-balance` → `data.balance`.
2. `sell_ratio` semantics in `buy_amount` mode: TP 50 plus trailing 50 should equal the whole position.
3. Whether condition orders are supported and correctly placed on robinhood (the docs say not supported only on arc/stable).
4. Robinhood gas: no gas flags passed (defaults).
5. The DexScreener orders endpoint response shape for robinhood.
6. A local fake-CLI end-to-end test was written but **blocked by the sandbox** — rerun it on the server:
   - put a fake `node_modules/.bin/gmgn-cli` that echoes JSON;
   - run `DRY_RUN=1 POLL_SEC=1 HOLD_HOURS=0.0008 node bot.mjs`.

## 8. How to re-run the analysis

```bash
pip install numpy
cd pumpscan/analysis
python3 forward.py ../data/scan            # needs a lot of RAM on the full 417 MB set
python3 export_trades.py ../data/scan ../results/forward_trades.csv F4 H4 Q4
python3 paid.py ../data/scan               # DEX-paid study
```
Simulation constants are in `explore.py`:
- `COST` 3%, `SIZE` $150, `CAP` +900%;
- `RUG_LIQ` $3K, `RUG_FRAC` 0.2;
- `MIN_LIQ` $5K, `MIN_VOL24` $10K.

## 9. Next steps (agreed with the user)

The detailed, checkbox version is `pumpscan/ROADMAP.md` (section ج). New finding of 10-04: split per chain, **F3 and F5 are profitable on Robinhood alone** (+183u / +194u, still positive with a 5-min delay), but that split was chosen after seeing the data, so they were frozen as **R3/R5** (2026-10-04 12:00 UTC) and must be confirmed on new scanner data before real money. See `FILTERS.md`.

1. **On the server:** install Node 22, clone (sparse), `npm install` in `pumpscan/bot`, create the GMGN API key, then fill `.env`. See `pumpscan/bot/README-fa.md`.
2. Run **DRY_RUN for at least a day**. Verify the signals match F4 and fix any JSON-path mismatches (section 7).
3. Go live with the **smallest size**. Confirm in GMGN that TP / trailing / SL were attached correctly.
4. Compare live fills with `results/forward_trades.csv` behaviour (win rate about 60%, median about +16%).
5. **Path 3 (user's preference long-term): direct-wallet bot without GMGN** (saves the 1% fee).
   - Sign swaps on Robinhood Chain with the user's own key: viem/ethers, Uniswap v3/v4 router / launchpad contracts. Robinhood RPC, chain id and router addresses must be researched from official sources.
   - Keep using GeckoTerminal / DexScreener for the F4 signal: the scanner already computes everything except GMGN's exact "ATH MC" (use the highest MC seen, or GeckoTerminal OHLCV).
   - Implement TP / trailing / SL / time exit in the bot itself.
6. Optionally keep the scanner running on the server (Robinhood only) to re-validate F4 every week. Market regimes change.
