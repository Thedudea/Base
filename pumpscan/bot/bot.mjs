// F4 auto-trader for Robinhood Chain through GMGN's official CLI (gmgn-cli).
//
// Every POLL_SEC it asks GMGN Trending (1h window) for tokens matching the F4 filter that won
// the one-week forward test, and buys each token the FIRST time it shows up, once, with a fixed
// amount. Take-profit / trailing / stop-loss orders are attached to the buy on GMGN's side; the
// bot itself sells whatever is left after HOLD_HOURS. DRY_RUN=1 (the default) only logs and
// notifies what it would buy.
//
//   cp .env.example .env   # fill in
//   npm install
//   node bot.mjs
//
// Telegram (optional): notifications plus /status /pause /resume /help from your own chat.

import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import 'dotenv/config';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(HERE, 'node_modules', '.bin', 'gmgn-cli');
const STATE_FILE = path.join(HERE, 'state.json');
const TRADES_CSV = path.join(HERE, 'trades.csv');
const ETH = '0x0000000000000000000000000000000000000000';
const CHAIN = 'robinhood';

const env = (k, d) => (process.env[k] ?? '').trim() || d;
const num = (k, d) => Number(env(k, String(d)));
const C = {
  dryRun: env('DRY_RUN', '1') !== '0',
  wallet: env('WALLET', ''),
  buyEth: env('BUY_ETH', '0.002'),
  maxBuysPerDay: num('MAX_BUYS_PER_DAY', 20),
  maxOpen: num('MAX_OPEN_POSITIONS', 10),
  slippage: num('SLIPPAGE', 15),
  pollSec: num('POLL_SEC', 60),
  holdHours: num('HOLD_HOURS', 6),
  buyOnStart: env('BUY_ON_START', '0') === '1',
  requireDexPaid: env('REQUIRE_DEX_PAID', '0') === '1',
  // F4 filter (GMGN Trending, 1h window)
  minLiq: num('F_MIN_LIQ', 70000),
  maxAthMc: num('F_MAX_ATH_MC', 160000),
  minVol: num('F_MIN_VOL_1H', 70000),
  maxTxs: num('F_MAX_TXS_1H', 900),
  // exits (attached to the buy as GMGN condition orders)
  tpPct: env('TP_PCT', '100'),
  tpSell: env('TP_SELL', '50'),
  trailDd: env('TRAIL_DD', '35'),
  slPct: env('SL_PCT', '35'),
  tgToken: env('TELEGRAM_BOT_TOKEN', ''),
  tgChat: env('TELEGRAM_CHAT_ID', ''),
};

const log = (...a) => console.log(new Date().toISOString().slice(0, 19).replace('T', ' '), ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- state

let S = { seen: {}, positions: {}, buysByDay: {}, paused: false, tgOffset: 0, started: false };
try {
  S = { ...S, ...JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')) };
} catch {}
const save = () => {
  fs.writeFileSync(STATE_FILE + '.tmp', JSON.stringify(S, null, 1));
  fs.renameSync(STATE_FILE + '.tmp', STATE_FILE);
};
const today = () => new Date().toISOString().slice(0, 10);
const csv = (row) => {
  if (!fs.existsSync(TRADES_CSV)) fs.writeFileSync(TRADES_CSV, 'time,mode,action,token,symbol,detail\n');
  fs.appendFileSync(TRADES_CSV, row.map((x) => String(x ?? '').replace(/[,\n]/g, ' ')).join(',') + '\n');
};

// ---------------------------------------------------------------- gmgn-cli

function cli(args, { trade = false, timeoutMs = 60_000 } = {}) {
  const e = { ...process.env };
  if (trade) e.GMGN_ALLOW_AUTOMATED_TRADES = '1';
  else delete e.GMGN_ALLOW_AUTOMATED_TRADES;
  return new Promise((resolve, reject) => {
    execFile(CLI, [...args, '--raw'], { env: e, timeout: timeoutMs, maxBuffer: 32 << 20 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(`gmgn-cli ${args.slice(0, 2).join(' ')} failed: ${(stderr || err.message).trim().slice(0, 400)}`));
      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error(`gmgn-cli ${args.slice(0, 2).join(' ')}: non-JSON output: ${stdout.slice(0, 200)}`));
      }
    });
  });
}

const rows = (j) => j?.data?.rank ?? j?.rank ?? j?.data ?? (Array.isArray(j) ? j : []);

async function trending() {
  const j = await cli(['market', 'trending', '--chain', CHAIN, '--interval', '1h', '--limit', '100',
    '--order-by', 'volume', '--filter', 'not_honeypot',
    '--min-liquidity', String(C.minLiq), '--max-history-highest-marketcap', String(C.maxAthMc),
    '--min-volume', String(C.minVol), '--max-swaps', String(C.maxTxs)]);
  // re-check every condition locally, so a server-side filter change can never widen what we buy
  return rows(j).filter((t) => t?.address
    && Number(t.liquidity) >= C.minLiq
    && Number(t.history_highest_market_cap) < C.maxAthMc
    && Number(t.volume) >= C.minVol
    && Number(t.swaps) < C.maxTxs
    && !(t.is_honeypot === true || t.is_honeypot === 1 || t.is_honeypot === '1'));
}

// DexScreener's own endpoint for paid orders on a token (profile = "DEX paid")
async function dexPaid(addr) {
  try {
    const r = await fetch(`https://api.dexscreener.com/orders/v1/${CHAIN}/${addr}`, { signal: AbortSignal.timeout(10_000) });
    if (!r.ok) return null;
    const j = await r.json();
    const list = Array.isArray(j) ? j : j?.orders ?? [];
    return list.some((o) => o?.type === 'tokenProfile' && o?.status === 'approved');
  } catch {
    return null;
  }
}

function toWei(eth) {
  const [i, f = ''] = String(eth).split('.');
  return (BigInt(i || '0') * 10n ** 18n + BigInt((f + '0'.repeat(18)).slice(0, 18))).toString();
}

const conditionOrders = () => JSON.stringify([
  { order_type: 'profit_stop', side: 'sell', price_scale: C.tpPct, sell_ratio: C.tpSell },
  { order_type: 'profit_stop_trace', side: 'sell', price_scale: C.tpPct, sell_ratio: String(100 - Number(C.tpSell)), drawdown_rate: C.trailDd },
  { order_type: 'loss_stop', side: 'sell', price_scale: C.slPct, sell_ratio: '100' },
]);

async function waitOrder(orderId) {
  for (let i = 0; i < 20; i++) {
    await sleep(3000);
    try {
      const j = await cli(['order', 'get', '--chain', CHAIN, '--order-id', orderId]);
      const st = (j?.data ?? j)?.status;
      if (['confirmed', 'successful', 'failed', 'expired'].includes(st)) return j?.data ?? j;
    } catch (e) {
      log('order get:', e.message);
    }
  }
  return null;
}

async function buy(t) {
  const j = await cli(['swap', '--chain', CHAIN, '--from', C.wallet, '--input-token', ETH, '--output-token', t.address,
    '--amount', toWei(C.buyEth), '--slippage', String(C.slippage), '--condition-orders', conditionOrders(),
    '--sell-ratio-type', 'buy_amount', '--yes'], { trade: true });
  const d = j?.data ?? j;
  const res = d?.order_id ? await waitOrder(d.order_id) : null;
  return { orderId: d?.order_id, status: res?.status ?? d?.status, report: res?.report };
}

async function sellAll(addr) {
  const bal = await cli(['portfolio', 'token-balance', '--chain', CHAIN, '--wallet', C.wallet, '--token', addr]).catch(() => null);
  const amount = Number((bal?.data ?? bal)?.balance ?? (bal?.data ?? bal)?.amount ?? NaN);
  if (amount === 0) return { status: 'nothing left (TP/SL already sold)' };
  const j = await cli(['swap', '--chain', CHAIN, '--from', C.wallet, '--input-token', addr, '--output-token', ETH,
    '--percent', '100', '--slippage', String(C.slippage), '--yes'], { trade: true });
  const d = j?.data ?? j;
  const res = d?.order_id ? await waitOrder(d.order_id) : null;
  return { orderId: d?.order_id, status: res?.status ?? d?.status };
}

// ---------------------------------------------------------------- telegram

async function tg(text) {
  if (!C.tgToken || !C.tgChat) return;
  try {
    await fetch(`https://api.telegram.org/bot${C.tgToken}/sendMessage`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: C.tgChat, text, disable_web_page_preview: true }),
      signal: AbortSignal.timeout(10_000),
    });
  } catch (e) {
    log('telegram:', e.message);
  }
}

async function tgCommands() {
  if (!C.tgToken || !C.tgChat) return;
  try {
    const r = await fetch(`https://api.telegram.org/bot${C.tgToken}/getUpdates?timeout=0&offset=${S.tgOffset}`, { signal: AbortSignal.timeout(10_000) });
    const j = await r.json();
    for (const u of j?.result ?? []) {
      S.tgOffset = u.update_id + 1;
      const m = u.message;
      if (!m || String(m.chat?.id) !== String(C.tgChat)) continue; // only your own chat can control the bot
      const cmd = (m.text || '').trim().split(/\s+/)[0];
      if (cmd === '/pause') { S.paused = true; await tg('⏸ ربات متوقف شد (خرید جدید انجام نمی‌شود؛ فروش‌های ۶ ساعته ادامه دارد).'); }
      else if (cmd === '/resume') { S.paused = false; await tg('▶️ ربات دوباره فعال شد.'); }
      else if (cmd === '/status') await tg(statusText());
      else if (cmd === '/help' || cmd === '/start') await tg('دستورها: /status /pause /resume');
    }
    save();
  } catch (e) {
    log('telegram updates:', e.message);
  }
}

const tokenLink = (a) => `https://gmgn.ai/robinhood/token/${a}`;
const statusText = () => {
  const open = Object.values(S.positions);
  return [`حالت: ${C.dryRun ? 'آزمایشی (بدون خرید واقعی)' : 'واقعی'}${S.paused ? ' — متوقف' : ''}`,
    `خرید امروز: ${S.buysByDay[today()] ?? 0} از ${C.maxBuysPerDay}`,
    `پوزیشن‌های باز: ${open.length}`,
    ...open.map((p) => `• ${p.symbol} — ${((Date.now() - p.t) / 3.6e6).toFixed(1)} ساعت`)].join('\n');
};

// ---------------------------------------------------------------- main loop

async function cycle() {
  await tgCommands();

  // 1) time exit for open positions
  for (const [addr, p] of Object.entries(S.positions)) {
    if (Date.now() - p.t < C.holdHours * 3.6e6) continue;
    if (C.dryRun) {
      log(`[dry] would sell rest of ${p.symbol} after ${C.holdHours}h`);
      csv([new Date().toISOString(), 'dry', 'time-exit', addr, p.symbol, '']);
    } else {
      try {
        const r = await sellAll(addr);
        log(`time-exit ${p.symbol}:`, r.status);
        csv([new Date().toISOString(), 'live', 'time-exit', addr, p.symbol, r.status]);
        await tg(`⏱ فروش ${C.holdHours} ساعته: ${p.symbol} — ${r.status}\n${tokenLink(addr)}`);
      } catch (e) {
        log('time-exit failed:', e.message);
        await tg(`⚠️ فروش ${p.symbol} انجام نشد: ${e.message.slice(0, 200)}`);
        continue; // retry next cycle
      }
    }
    delete S.positions[addr];
    save();
  }

  // 2) new signals
  const list = await trending();
  const firstRun = !S.started;
  for (const t of list) {
    const addr = t.address.toLowerCase();
    if (S.seen[addr]) continue;
    S.seen[addr] = Date.now();
    if (firstRun && !C.buyOnStart) continue; // already in the list when the bot started: not a fresh signal
    const sym = t.symbol || addr.slice(0, 8);
    const info = `MC $${Math.round(t.market_cap / 1e3)}K · Liq $${Math.round(t.liquidity / 1e3)}K · Vol1h $${Math.round(t.volume / 1e3)}K · TXs ${t.swaps}`;
    log(`signal ${sym} ${addr} ${info}`);

    if (C.requireDexPaid) {
      const paid = await dexPaid(addr);
      if (paid !== true) {
        log(`  skip: DEX paid = ${paid}`);
        csv([new Date().toISOString(), C.dryRun ? 'dry' : 'live', 'skip-not-paid', addr, sym, info]);
        continue;
      }
    }
    const nToday = S.buysByDay[today()] ?? 0;
    const why = S.paused ? 'paused' : nToday >= C.maxBuysPerDay ? 'daily buy limit' : Object.keys(S.positions).length >= C.maxOpen ? 'max open positions' : '';
    if (why) {
      log(`  skip: ${why}`);
      await tg(`🔎 سیگنال: ${sym} (خریده نشد: ${why})\n${info}\n${tokenLink(addr)}`);
      continue;
    }
    if (C.dryRun) {
      csv([new Date().toISOString(), 'dry', 'buy', addr, sym, info]);
      await tg(`🧪 [آزمایشی] خرید می‌شد: ${sym} — ${C.buyEth} ETH\n${info}\n${tokenLink(addr)}`);
      S.positions[addr] = { t: Date.now(), symbol: sym };
    } else {
      try {
        const r = await buy(t);
        log(`  bought ${sym}:`, r.status, r.orderId);
        csv([new Date().toISOString(), 'live', 'buy', addr, sym, `${r.status} ${r.orderId} ${info}`]);
        await tg(`✅ خرید ${sym} — ${C.buyEth} ETH (${r.status})\nTP +${C.tpPct}% فروش ${C.tpSell}% · تریلینگ ${C.trailDd}% · SL -${C.slPct}%\n${info}\n${tokenLink(addr)}`);
        if (r.status === 'failed' || r.status === 'expired') continue;
        S.positions[addr] = { t: Date.now(), symbol: sym, orderId: r.orderId };
      } catch (e) {
        log('  buy failed:', e.message);
        await tg(`⚠️ خرید ${sym} انجام نشد: ${e.message.slice(0, 200)}`);
        continue;
      }
    }
    S.buysByDay[today()] = nToday + 1;
    save();
  }
  if (firstRun) {
    S.started = true;
    log(`started: ${Object.keys(S.seen).length} tokens already in the list are ignored (BUY_ON_START=0)`);
  }
  save();
}

async function main() {
  if (!fs.existsSync(CLI)) throw new Error('gmgn-cli not installed — run `npm install` in this folder');
  if (!C.wallet && !C.dryRun) throw new Error('WALLET is required when DRY_RUN=0');
  log(`F4 bot on ${CHAIN} — ${C.dryRun ? 'DRY RUN (no real trades)' : 'LIVE'} — ${C.buyEth} ETH per buy, max ${C.maxBuysPerDay}/day`);
  await tg(`🤖 ربات روشن شد — ${C.dryRun ? 'حالت آزمایشی' : 'حالت واقعی'} — ${C.buyEth} ETH برای هر خرید`);
  for (;;) {
    const t0 = Date.now();
    try {
      await cycle();
    } catch (e) {
      log('cycle error:', e.message);
      if (/429|rate/i.test(e.message)) await sleep(30_000);
    }
    await sleep(Math.max(1000, C.pollSec * 1000 - (Date.now() - t0)));
  }
}

main().catch(async (e) => {
  log('fatal:', e.message);
  await tg(`⛔️ ربات متوقف شد: ${e.message}`);
  process.exit(1);
});
