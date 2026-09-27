// Pulls public market history for a list of tokens that pumped, so the offline
// analysis can look at what they looked like *before* the move. Runs on GitHub
// Actions (the dev sandbox has no egress to these APIs).
//
//   node fetch.mjs <outDir>
//
// Sources (all public, no keys):
//   - DexScreener: current pairs, liquidity, volume, txns, socials, pair age
//   - GeckoTerminal: every pool of the token + OHLCV (day / hour / 5m / 1m)
//   - Solana only: pump.fun coin info, mint account, largest holders

import fs from 'node:fs';
import path from 'node:path';

const cfg = JSON.parse(fs.readFileSync(new URL('./config.json', import.meta.url)));
const OUT = process.argv[2] || 'out';
const GT = 'https://api.geckoterminal.com/api/v2';
const RPCS = [...(process.env.RPC_URLS || '').split(',').filter(Boolean), 'https://solana-rpc.publicnode.com', 'https://api.mainnet-beta.solana.com'];

const T0 = Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(`[${((Date.now() - T0) / 1000).toFixed(0)}s]`, ...a);
const errors = [];

let lastGT = 0;
async function get(url, { gt = false, tries = 4, init = {} } = {}) {
  for (let i = 0; i < tries; i++) {
    if (gt) {
      // GeckoTerminal public limit is ~30 calls/min
      const wait = lastGT + 2300 - Date.now();
      if (wait > 0) await sleep(wait);
      lastGT = Date.now();
    }
    try {
      const r = await fetch(url, {
        ...init,
        headers: { accept: 'application/json', 'user-agent': 'Mozilla/5.0 (pumpscan research)', ...(init.headers || {}) },
        signal: AbortSignal.timeout(30000),
      });
      const txt = await r.text();
      if (r.status === 429 || r.status >= 500) {
        errors.push({ url, status: r.status });
        await sleep(6000 * (i + 1));
        continue;
      }
      if (!r.ok) {
        errors.push({ url, status: r.status, body: txt.slice(0, 300) });
        return null;
      }
      try {
        return JSON.parse(txt);
      } catch {
        errors.push({ url, status: r.status, nonJson: txt.slice(0, 300) });
        return null;
      }
    } catch (e) {
      errors.push({ url, err: String(e) });
      await sleep(3000 * (i + 1));
    }
  }
  return null;
}

async function rpc(method, params) {
  for (const url of RPCS) {
    const j = await get(url, {
      tries: 2,
      init: { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) },
    });
    if (j && !j.error) return j.result;
    if (j?.error) errors.push({ rpc: url, method, error: j.error });
  }
  return null;
}

const save = (dir, name, obj) => obj != null && fs.writeFileSync(path.join(dir, name), JSON.stringify(obj));

// Candles are [ts, open, high, low, close, volumeUsd]. Pages go backwards in time.
async function ohlcv(net, pool, token, tf, agg, maxPages, since) {
  const byTs = new Map();
  let before = Math.floor(Date.now() / 1000) + 60;
  for (let p = 0; p < maxPages; p++) {
    const j = await get(
      `${GT}/networks/${net}/pools/${pool}/ohlcv/${tf}?aggregate=${agg}&limit=1000&before_timestamp=${before}&currency=usd&token=${token}`,
      { gt: true },
    );
    const list = j?.data?.attributes?.ohlcv_list || [];
    for (const c of list) byTs.set(c[0], c);
    if (!list.length) break;
    const oldest = Math.min(...list.map((c) => c[0]));
    if (oldest <= since || list.length < 1000) break;
    before = oldest;
  }
  return [...byTs.values()].sort((a, b) => a[0] - b[0]);
}

async function doToken(addr) {
  const dir = path.join(OUT, addr);
  fs.mkdirSync(dir, { recursive: true });
  log('token', addr);

  save(dir, 'dexscreener.json', await get(`https://api.dexscreener.com/latest/dex/tokens/${addr}`));

  // which network is it on? (also finds chains we don't know the GeckoTerminal id of)
  const search = await get(`${GT}/search/pools?query=${addr}`, { gt: true });
  save(dir, 'gt_search.json', search);
  const nets = [...new Set((search?.data || []).map((p) => p.relationships?.network?.data?.id).filter(Boolean))];
  const meta = { addr, nets, pools: [] };

  for (const net of nets) {
    const id = net === 'solana' ? addr : addr.toLowerCase();
    save(dir, `gt_token_${net}.json`, await get(`${GT}/networks/${net}/tokens/${id}`, { gt: true }));
    save(dir, `gt_info_${net}.json`, await get(`${GT}/networks/${net}/tokens/${id}/info`, { gt: true }));
    const pools = [];
    for (let page = 1; page <= 2; page++) {
      const j = await get(`${GT}/networks/${net}/tokens/${id}/pools?page=${page}`, { gt: true });
      pools.push(...(j?.data || []));
      if (!j?.data || j.data.length < 20) break;
    }
    save(dir, `gt_pools_${net}.json`, pools);

    // the most liquid pool, the most traded one and the oldest one (often the launchpad curve)
    const a = (p) => p.attributes;
    const pick = new Map();
    const by = (f) => [...pools].sort(f)[0];
    for (const p of [
      by((x, y) => Number(a(y).reserve_in_usd || 0) - Number(a(x).reserve_in_usd || 0)),
      by((x, y) => Number(a(y).volume_usd?.h24 || 0) - Number(a(x).volume_usd?.h24 || 0)),
      by((x, y) => new Date(a(x).pool_created_at) - new Date(a(y).pool_created_at)),
    ]) if (p) pick.set(a(p).address, p);

    for (const p of pick.values()) {
      const pool = a(p).address;
      const since = Math.floor(new Date(a(p).pool_created_at || 0).getTime() / 1000);
      log(' pool', net, pool, a(p).name, a(p).pool_created_at);
      const candles = {
        day: await ohlcv(net, pool, id, 'day', 1, 1, since),
        hour: await ohlcv(net, pool, id, 'hour', 1, cfg.hourPages, since),
        m5: await ohlcv(net, pool, id, 'minute', 5, cfg.m5Pages, since),
        m1: await ohlcv(net, pool, id, 'minute', 1, cfg.m1Pages, since),
      };
      const trades = await get(`${GT}/networks/${net}/pools/${pool}/trades`, { gt: true });
      save(dir, `ohlcv_${net}_${pool}.json`, { pool: p, candles, trades: trades?.data || null });
      meta.pools.push({ net, pool, name: a(p).name, dex: p.relationships?.dex?.data?.id, created: a(p).pool_created_at, n: Object.fromEntries(Object.entries(candles).map(([k, v]) => [k, v.length])) });
    }
  }

  if (nets.includes('solana') || !addr.startsWith('0x')) {
    save(dir, 'pumpfun.json', await get(`https://frontend-api-v3.pump.fun/coins/${addr}`));
    save(dir, 'mint_account.json', await rpc('getAccountInfo', [addr, { encoding: 'jsonParsed' }]));
    save(dir, 'largest_holders.json', await rpc('getTokenLargestAccounts', [addr]));
  }
  save(dir, 'meta.json', meta);
}

fs.mkdirSync(OUT, { recursive: true });
for (const t of cfg.tokens) {
  try {
    await doToken(t);
  } catch (e) {
    errors.push({ token: t, err: String(e?.stack || e) });
  }
}
fs.writeFileSync(path.join(OUT, 'errors.json'), JSON.stringify(errors, null, 1));
log('done, errors:', errors.length);
