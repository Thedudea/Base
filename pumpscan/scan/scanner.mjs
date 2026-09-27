// Market scanner. Every cycle (default 5 min) it
//   1. records the trending / new pool lists of GeckoTerminal and the boosted /
//      newly profiled tokens of DexScreener for the configured chains, and
//   2. re-snapshots every token it is tracking (DexScreener pair data), so the
//      offline analysis can label what happened *after* a token showed up and
//      learn which of its visible stats predicted a pump.
//
//   node scanner.mjs <outDir>          (env: DURATION_MIN, CYCLE_SEC)
//
// Output: <outDir>/<YYYY-MM-DD>/<HHMM>.ndjson.gz, one JSON array per line
// (the row layouts are in schema.json), plus state.json with the tracked set.
// No keys, no wallets, read-only public APIs; runs on GitHub Actions or any box
// with Node 22.

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const cfg = JSON.parse(fs.readFileSync(new URL('./config.json', import.meta.url)));
const OUT = process.argv[2] || 'scan-out';
const DURATION = Number(process.env.DURATION_MIN || cfg.durationMin) * 60_000;
const CYCLE = Number(process.env.CYCLE_SEC || cfg.cycleSec) * 1000;
const GT = 'https://api.geckoterminal.com/api/v2';
const DS = 'https://api.dexscreener.com';
const HOUR = 3600_000;

const T0 = Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(`[${new Date().toISOString().slice(11, 19)}]`, ...a);

// ------------------------------------------------------------------ schema

const SCHEMA = {
  g: ['type', 't', 'chain', 'token', 'pool', 'list', 'rank', 'fdv', 'mc', 'liq', 'poolCreated', 'dex',
    'chM5', 'chM15', 'chM30', 'chH1', 'chH6', 'chH24',
    'volM5', 'volM15', 'volM30', 'volH1', 'volH6', 'volH24',
    'buysM5', 'sellsM5', 'buyersM5', 'sellersM5', 'buysH1', 'sellsH1', 'buyersH1', 'sellersH1',
    'buysH6', 'sellsH6', 'buyersH6', 'sellersH6', 'buysH24', 'sellsH24', 'buyersH24', 'sellersH24'],
  d: ['type', 't', 'chain', 'token', 'pair', 'dex', 'price', 'mc', 'fdv', 'liq', 'pairCreated', 'nPairs',
    'volM5', 'volH1', 'volH6', 'volH24', 'volAllH1', 'volAllH24', 'liqAll',
    'buysM5', 'sellsM5', 'buysH1', 'sellsH1', 'buysH6', 'sellsH6', 'buysH24', 'sellsH24',
    'chM5', 'chH1', 'chH6', 'chH24', 'boosts'],
  m: ['type', 't', 'chain', 'token', 'name', 'symbol', 'socials', 'nWebsites', 'hasImage', 'firstSource'],
  b: ['type', 't', 'chain', 'token', 'kind', 'amount'],
  x: ['type', 't', 'list', 'item'],
};

// ------------------------------------------------------------------ http

const hosts = { gt: { gap: 3500, next: 0 }, ds: { gap: 230, next: 0 }, gmgn: { gap: 4000, next: 0 } };
const errs = {};
const noteErr = (k) => (errs[k] = (errs[k] || 0) + 1);

async function get(url, host, tries = 3) {
  const h = hosts[host];
  for (let i = 0; i < tries; i++) {
    const wait = h.next - Date.now();
    if (wait > 0) await sleep(wait);
    h.next = Date.now() + h.gap;
    try {
      const r = await fetch(url, {
        headers: { accept: 'application/json', 'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) pumpscan-research' },
        signal: AbortSignal.timeout(25_000),
      });
      if (r.status === 429 || r.status >= 500) {
        noteErr(`${host} ${r.status}`);
        h.next = Date.now() + 8000 * (i + 1);
        continue;
      }
      if (!r.ok) {
        noteErr(`${host} ${r.status}`);
        return null;
      }
      return await r.json();
    } catch (e) {
      noteErr(`${host} ${e.name}`);
      await sleep(2000 * (i + 1));
    }
  }
  return null;
}

// ------------------------------------------------------------------ output

let rowsThisCycle = [];
const emit = (row) => rowsThisCycle.push(row);

function flushRows(t) {
  if (!rowsThisCycle.length) return;
  const d = new Date(t);
  const day = d.toISOString().slice(0, 10);
  const hhmm = d.toISOString().slice(11, 13) + (d.getUTCMinutes() < 30 ? '00' : '30');
  const dir = path.join(OUT, day);
  fs.mkdirSync(dir, { recursive: true });
  // gzip members concatenate into a valid gzip stream
  fs.appendFileSync(path.join(dir, `${hhmm}.ndjson.gz`), zlib.gzipSync(rowsThisCycle.map((r) => JSON.stringify(r)).join('\n') + '\n'));
  rowsThisCycle = [];
}

// ------------------------------------------------------------------ state

const statePath = path.join(OUT, 'state.json');
fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(path.join(OUT, 'schema.json'), JSON.stringify(SCHEMA, null, 1));
let state = { tokens: {} };
try {
  state = JSON.parse(fs.readFileSync(statePath));
} catch {}
const saveState = () => {
  fs.writeFileSync(statePath + '.tmp', JSON.stringify(state));
  fs.renameSync(statePath + '.tmp', statePath);
};

const norm = (a) => (a.startsWith('0x') ? a.toLowerCase() : a);
const key = (c, a) => `${c}:${norm(a)}`;
const num = (v) => (v == null || v === '' ? null : Number(Number(v).toPrecision(5)));
const sec = (iso) => (iso ? Math.floor(new Date(iso).getTime() / 1000) : null);

function track(t, chain, addr, source) {
  const k = key(chain, addr);
  let s = state.tokens[k];
  if (!s) s = state.tokens[k] = { c: chain, a: norm(addr), first: t, src: source, snap: 0, miss: 0 };
  if (source !== 'new') s.list = t; // appearing in a trending/boost list keeps a token hot
  return s;
}

// ------------------------------------------------------------------ discovery

function gtRow(t, net, p, list, rank) {
  const a = p.attributes || {};
  const base = p.relationships?.base_token?.data?.id || '';
  const addr = base.slice(base.indexOf('_') + 1);
  if (!addr) return null;
  const pc = a.price_change_percentage || {};
  const v = a.volume_usd || {};
  const tx = a.transactions || {};
  const q = (w) => [tx[w]?.buys, tx[w]?.sells, tx[w]?.buyers, tx[w]?.sellers].map(num);
  return ['g', t, net, norm(addr), a.address, list, rank, num(a.fdv_usd), num(a.market_cap_usd), num(a.reserve_in_usd),
    sec(a.pool_created_at), p.relationships?.dex?.data?.id || null,
    ...['m5', 'm15', 'm30', 'h1', 'h6', 'h24'].map((w) => num(pc[w])),
    ...['m5', 'm15', 'm30', 'h1', 'h6', 'h24'].map((w) => num(v[w])),
    ...q('m5'), ...q('h1'), ...q('h6'), ...q('h24')];
}

async function discover(t) {
  for (const net of cfg.chains) {
    for (const d of cfg.gtTrendingDurations) {
      for (let page = 1; page <= cfg.gtTrendingPages; page++) {
        const j = await get(`${GT}/networks/${net}/trending_pools?duration=${d}&page=${page}`, 'gt');
        (j?.data || []).forEach((p, i) => {
          const row = gtRow(t, net, p, `tr${d}`, (page - 1) * 20 + i + 1);
          if (row) emit(row), track(t, net, row[3], `tr${d}`);
        });
      }
    }
    for (let page = 1; page <= cfg.gtNewPages; page++) {
      const j = await get(`${GT}/networks/${net}/new_pools?page=${page}`, 'gt');
      (j?.data || []).forEach((p, i) => {
        const row = gtRow(t, net, p, 'new', (page - 1) * 20 + i + 1);
        // brand-new pools only get tracked once they have some liquidity
        if (row) emit(row), (row[9] || 0) >= cfg.newMinLiq && track(t, net, row[3], 'new');
      });
    }
  }
  for (const [kind, url] of [['boostLatest', '/token-boosts/latest/v1'], ['boostTop', '/token-boosts/top/v1'], ['profile', '/token-profiles/latest/v1']]) {
    const j = await get(DS + url, 'ds');
    for (const it of Array.isArray(j) ? j : []) {
      if (!cfg.chains.includes(it.chainId) || !it.tokenAddress) continue;
      emit(['b', t, it.chainId, norm(it.tokenAddress), kind, num(it.totalAmount ?? it.amount)]);
      track(t, it.chainId, it.tokenAddress, kind);
    }
  }
}

// GMGN's own lists would carry holders / top10 / dev stats. Probe once per run;
// if the site blocks server requests (likely), the scanner just goes without.
let gmgnOk = false;
async function gmgn(t, probe = false) {
  const lists = probe ? ['1h'] : ['5m', '1h'];
  for (const w of lists) {
    const j = await get(`https://gmgn.ai/defi/quotation/v1/rank/sol/swaps/${w}?orderby=swaps&direction=desc`, 'gmgn', 1);
    const items = j?.data?.rank;
    if (!Array.isArray(items)) return false;
    for (const it of items) {
      emit(['x', t, w, it]);
      if (it.address) track(t, 'solana', it.address, `gmgn${w}`);
    }
  }
  return true;
}

// ------------------------------------------------------------------ snapshots

const isHot = (s, t) => (s.list && t - s.list < cfg.hotHours * HOUR) || (s.hotUntil || 0) > t;

async function snapshot(t) {
  const due = Object.values(state.tokens).filter((s) => isHot(s, t) || t - s.snap >= cfg.coldEverySec * 1000 - 30_000);
  for (const chain of cfg.chains) {
    const list = due.filter((s) => s.c === chain);
    for (let i = 0; i < list.length; i += 30) {
      const chunk = list.slice(i, i + 30);
      const pairs = await get(`${DS}/tokens/v1/${chain}/${chunk.map((s) => s.a).join(',')}`, 'ds');
      if (!Array.isArray(pairs)) continue;
      const by = {};
      for (const p of pairs) {
        const a = norm(p.baseToken?.address || '');
        (by[a] ||= []).push(p);
      }
      for (const s of chunk) {
        const ps = by[s.a];
        if (!ps?.length) {
          s.miss++;
          continue;
        }
        s.miss = 0;
        s.snap = t;
        const liq = (p) => p.liquidity?.usd || 0;
        const top = ps.reduce((a, b) => (liq(b) > liq(a) ? b : a));
        const sum = (f) => num(ps.reduce((acc, p) => acc + (f(p) || 0), 0));
        const v = top.volume || {};
        const tx = top.txns || {};
        const ch = top.priceChange || {};
        emit(['d', t, chain, s.a, top.pairAddress, top.dexId, num(top.priceUsd), num(top.marketCap), num(top.fdv), num(liq(top)),
          top.pairCreatedAt ? Math.floor(top.pairCreatedAt / 1000) : null, ps.length,
          num(v.m5), num(v.h1), num(v.h6), num(v.h24), sum((p) => p.volume?.h1), sum((p) => p.volume?.h24), sum(liq),
          ...['m5', 'h1', 'h6', 'h24'].flatMap((w) => [num(tx[w]?.buys), num(tx[w]?.sells)]),
          ...['m5', 'h1', 'h6', 'h24'].map((w) => num(ch[w])), top.boosts?.active ?? null]);
        if (!s.meta) {
          const info = top.info || {};
          emit(['m', t, chain, s.a, top.baseToken?.name, top.baseToken?.symbol, (info.socials || []).map((x) => x.type),
            (info.websites || []).length, !!info.imageUrl, s.src]);
          s.meta = 1;
        }
        s.mc = top.marketCap || top.fdv || 0;
        s.vol24 = v.h24 || 0;
        if ((v.h1 || 0) >= cfg.hotVolH1 || Math.abs(ch.h1 || 0) >= cfg.hotChangeH1) s.hotUntil = t + 2 * HOUR;
      }
    }
  }
  return due.length;
}

function prune(t) {
  for (const [k, s] of Object.entries(state.tokens)) {
    const quiet = !isHot(s, t) && t - (s.list || s.first) > cfg.dropAfterQuietHours * HOUR && (s.vol24 || 0) < cfg.dropBelowVolH24;
    if (t - s.first > cfg.maxTrackDays * 24 * HOUR || s.miss >= 3 || quiet) delete state.tokens[k];
  }
  for (const chain of cfg.chains) {
    const cold = Object.entries(state.tokens).filter(([, s]) => s.c === chain);
    if (cold.length <= cfg.maxTrackedPerChain) continue;
    cold.filter(([, s]) => !isHot(s, t)).sort((a, b) => (a[1].vol24 || 0) - (b[1].vol24 || 0))
      .slice(0, cold.length - cfg.maxTrackedPerChain).forEach(([k]) => delete state.tokens[k]);
  }
}

// ------------------------------------------------------------------ main loop

gmgnOk = await gmgn(Date.now(), true);
log('gmgn reachable:', gmgnOk, 'tracked from previous runs:', Object.keys(state.tokens).length);
state.gmgnOk = gmgnOk;
flushRows(Date.now());

while (Date.now() - T0 < DURATION - CYCLE / 2) {
  const t = Date.now();
  await discover(t);
  if (gmgnOk) await gmgn(t);
  const n = await snapshot(t);
  prune(t);
  const nRows = rowsThisCycle.length;
  flushRows(t);
  saveState();
  fs.writeFileSync(path.join(OUT, 'errors-latest.json'), JSON.stringify({ at: new Date(t).toISOString(), errs }, null, 1));
  log(`cycle done in ${((Date.now() - t) / 1000).toFixed(0)}s: tracked ${Object.keys(state.tokens).length}, snapshotted ${n}, rows ${nRows}, errors`, JSON.stringify(errs));
  const wait = t + CYCLE - Date.now();
  if (wait > 0) await sleep(wait);
}
log('duration reached');
