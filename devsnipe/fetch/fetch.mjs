// Fetches on-chain history for one dev wallet: its transactions, the tokens it
// created, and every transaction on each of those tokens. Runs on GitHub
// Actions (the dev sandbox has no Solana egress). Output is raw RPC JSON so the
// Python analysis can re-parse it offline.
//
//   node fetch.mjs <devWallet> <outDir>
//
// Extra RPC endpoints can be passed as a comma separated RPC_URLS env var.

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const cfg = JSON.parse(fs.readFileSync(new URL('./config.json', import.meta.url)));
const DEV = process.argv[2];
const OUT = process.argv[3] || `out/${DEV}`;
const T0 = Date.now();
const BUDGET_MS = cfg.timeBudgetMin * 60_000;
const WSOL = 'So11111111111111111111111111111111111111112';

fs.mkdirSync(path.join(OUT, 'mints'), { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(`[${((Date.now() - T0) / 1000).toFixed(0)}s]`, ...a);
const overBudget = () => Date.now() - T0 > BUDGET_MS;

// ---------------------------------------------------------------- RPC pool

const CANDIDATES = [
  ...(process.env.RPC_URLS || '').split(',').filter(Boolean).map((url) => ({ url, rps: 20 })),
  { url: 'https://api.mainnet-beta.solana.com', rps: 2 },
  { url: 'https://solana-rpc.publicnode.com', rps: 12 },
  { url: 'https://solana.drpc.org', rps: 4 },
  { url: 'https://solana.api.onfinality.io/public', rps: 3 },
  { url: 'https://rpc.ankr.com/solana', rps: 4 },
];

class Endpoint {
  constructor({ url, rps }) {
    this.url = url;
    this.minInterval = 1000 / rps;
    this.interval = this.minInterval;
    this.next = 0;
    this.bad = new Set(); // methods this endpoint cannot serve
    this.ok = 0;
    this.err = 0;
    this.hardErr = {};
    this.okBy = {};
  }
}

let endpoints = [];

async function rawCall(ep, method, params, timeout = 30000) {
  const r = await fetch(ep.url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    signal: AbortSignal.timeout(timeout),
  });
  const text = await r.text();
  let j;
  try {
    j = JSON.parse(text);
  } catch {
    j = null;
  }
  return { status: r.status, j, text: text.slice(0, 300) };
}

const errLog = {}; // "url method" -> first messages seen
function noteErr(ep, method, msg) {
  const k = `${ep.url} ${method}`;
  const arr = (errLog[k] ||= []);
  if (arr.length < 15 && !arr.includes(msg)) {
    arr.push(msg);
    log('rpc error', k, msg);
  }
}

// nullMeansMissing: getTransaction returns null when a node lacks history, so
// try another endpoint before trusting the null.
async function rpc(method, params, { nullMeansMissing = false, tries = 12 } = {}) {
  const triedNull = new Set();
  let lastErr;
  for (let attempt = 0; attempt < tries; attempt++) {
    const candidates = endpoints.filter((e) => !e.bad.has(method) && !triedNull.has(e));
    if (!candidates.length) break;
    candidates.sort((a, b) => a.next - b.next);
    const ep = candidates[0];
    const now = Date.now();
    const wait = Math.max(0, ep.next - now);
    ep.next = Math.max(now, ep.next) + ep.interval;
    if (wait) await sleep(wait);
    ep.hardErr[method] ||= 0;
    ep.okBy[method] ||= 0;
    try {
      const { status, j, text } = await rawCall(ep, method, params);
      const em = j && j.error ? `${j.error.code} ${j.error.message}` : null;
      if (status === 429 || (j && j.error && (j.error.code === 429 || /rate|too many/i.test(j.error.message)))) {
        ep.interval = Math.min(ep.interval * 1.5, 6000);
        ep.next = Date.now() + 1000 * Math.min(attempt + 1, 6);
        ep.err++;
        lastErr = `429 ${ep.url} ${em || text}`;
        noteErr(ep, method, `429 ${(em || text).slice(0, 120)}`);
        continue;
      }
      if (status !== 200 || !j || j.error) {
        ep.err++;
        ep.hardErr[method]++;
        lastErr = `${ep.url} http=${status} ${em || text}`;
        noteErr(ep, method, `http=${status} ${(em || text).slice(0, 200)}`);
        ep.next = Date.now() + 500 * Math.min(attempt + 1, 6);
        // only give up on an endpoint for a method once it clearly cannot serve it
        const code = j && j.error && j.error.code;
        if (code === -32601 || (ep.hardErr[method] >= 12 && ep.okBy[method] < ep.hardErr[method] / 10)) {
          ep.bad.add(method);
          log(`endpoint ${ep.url} disabled for ${method}`);
        }
        continue;
      }
      ep.ok++;
      ep.okBy[method]++;
      ep.interval = Math.max(ep.minInterval, ep.interval * 0.97);
      if (j.result === null && nullMeansMissing) {
        triedNull.add(ep);
        continue;
      }
      return j.result;
    } catch (e) {
      ep.err++;
      lastErr = `${ep.url} ${e.message}`;
      noteErr(ep, method, `exception ${e.message}`);
      ep.next = Date.now() + 1000 * Math.min(attempt + 1, 6);
    }
  }
  if (nullMeansMissing) return null;
  throw new Error(`${method} failed: ${lastErr}`);
}

async function probe() {
  const results = [];
  // A known old pump.fun tx to test historical getTransaction support is not
  // needed: we test with a recent signature of the dev wallet itself.
  for (const c of CANDIDATES) {
    const ep = new Endpoint(c);
    const res = { url: c.url };
    try {
      const t = Date.now();
      const { status, j, text } = await rawCall(ep, 'getSlot', [], 10000);
      res.getSlot = { status, ms: Date.now() - t, result: j && (j.result ?? j.error), text: j ? undefined : text };
      const s = await rawCall(ep, 'getSignaturesForAddress', [DEV, { limit: 5 }], 15000);
      res.getSigs = { status: s.status, n: s.j && Array.isArray(s.j.result) ? s.j.result.length : null, err: s.j && s.j.error };
      if (s.j && Array.isArray(s.j.result) && s.j.result.length) {
        const sig = s.j.result[s.j.result.length - 1].signature;
        const tx = await rawCall(ep, 'getTransaction', [sig, { encoding: 'json', maxSupportedTransactionVersion: 1 }], 15000);
        res.getTx = { status: tx.status, ok: !!(tx.j && tx.j.result), err: tx.j && tx.j.error };
      }
    } catch (e) {
      res.error = e.message;
    }
    results.push(res);
    const usable = res.getSlot && res.getSlot.status === 200 && typeof res.getSlot.result === 'number';
    if (usable) {
      if (!(res.getSigs && res.getSigs.n)) ep.bad.add('getSignaturesForAddress');
      if (!(res.getTx && res.getTx.ok)) ep.bad.add('getTransaction');
      endpoints.push(ep);
    }
  }
  fs.writeFileSync(path.join(OUT, 'probe.json'), JSON.stringify(results, null, 1));
  log('endpoints', endpoints.map((e) => `${e.url} bad=[${[...e.bad]}]`));
  if (!endpoints.length) throw new Error('no RPC endpoint reachable');
}

// ---------------------------------------------------------------- helpers

async function allSigs(address, max) {
  const out = [];
  let before;
  while (out.length < max) {
    const limit = Math.min(1000, max - out.length);
    const page = await rpc('getSignaturesForAddress', [address, { limit, ...(before ? { before } : {}) }]);
    if (!page || !page.length) break;
    out.push(...page);
    before = page[page.length - 1].signature;
    if (page.length < limit) break;
  }
  return out;
}

async function pool(items, n, fn) {
  const res = new Array(items.length);
  let i = 0;
  await Promise.all(
    Array.from({ length: n }, async () => {
      while (i < items.length) {
        const k = i++;
        res[k] = await fn(items[k], k);
      }
    }),
  );
  return res;
}

let shapeLogged = false;
function logShape(tx) {
  if (shapeLogged || !tx || tx.version !== 1) return;
  shapeLogged = true;
  fs.writeFileSync(path.join(OUT, 'sample_v1_tx.json'), JSON.stringify(tx, null, 1));
  log('v1 tx keys', Object.keys(tx), 'message keys', Object.keys(tx.transaction?.message || {}));
}

const getTx = (sig) =>
  rpc('getTransaction', [sig, { encoding: 'json', maxSupportedTransactionVersion: 1, commitment: 'confirmed' }], {
    nullMeansMissing: true,
  }).then((tx) => {
    logShape(tx);
    return tx;
  });

function writeJsonlGz(file, rows) {
  const body = rows.map((r) => JSON.stringify(r)).join('\n');
  fs.writeFileSync(file, zlib.gzipSync(body, { level: 9 }));
}

function accountKeys(tx) {
  const m = tx.transaction.message;
  const la = tx.meta && tx.meta.loadedAddresses;
  return [...m.accountKeys, ...(la ? [...la.writable, ...la.readonly] : [])];
}

// A creation tx: signed by the dev and initialises a new mint.
function creationInfo(tx) {
  if (!tx || !tx.meta || tx.meta.err) return null;
  const logs = tx.meta.logMessages || [];
  if (!logs.some((l) => /Instruction: InitializeMint/.test(l))) return null;
  const pre = new Set((tx.meta.preTokenBalances || []).map((b) => b.mint));
  const mints = [...new Set((tx.meta.postTokenBalances || []).map((b) => b.mint))].filter(
    (m) => m !== WSOL && !pre.has(m),
  );
  if (!mints.length) return null;
  const keys = accountKeys(tx);
  const nSigners = tx.transaction.message.header.numRequiredSignatures;
  const signers = keys.slice(0, nSigners);
  if (!signers.includes(DEV)) return null;
  const programs = [...new Set(tx.transaction.message.instructions.map((ix) => keys[ix.programIdIndex]))];
  return { mint: mints[0], slot: tx.slot, blockTime: tx.blockTime, signature: tx.transaction.signatures[0], programs, signers };
}

// pump.fun public API; often Cloudflare-blocked, so strictly best effort.
async function pumpApi(url) {
  try {
    const r = await fetch(url, {
      headers: {
        accept: 'application/json',
        origin: 'https://pump.fun',
        referer: 'https://pump.fun/',
        'user-agent':
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36',
      },
      signal: AbortSignal.timeout(20000),
    });
    const t = await r.text();
    if (r.status !== 200) return { status: r.status, body: t.slice(0, 200) };
    return { status: 200, data: JSON.parse(t) };
  } catch (e) {
    return { error: e.message };
  }
}

// ---------------------------------------------------------------- main

async function main() {
  const meta = { dev: DEV, startedAt: new Date().toISOString(), cfg, errors: [] };
  await probe();

  // pump.fun API (best effort)
  const pf = await pumpApi(
    `https://frontend-api-v3.pump.fun/coins/user-created-coins/${DEV}?offset=0&limit=100&includeNsfw=true`,
  );
  fs.writeFileSync(path.join(OUT, 'pumpfun_created.json'), JSON.stringify(pf));
  log('pump.fun api user-created-coins status', pf.status ?? pf.error);

  // 1. dev wallet signatures
  const devSigs = await allSigs(DEV, cfg.maxDevSigs);
  fs.writeFileSync(path.join(OUT, 'dev_sigs.json'), JSON.stringify(devSigs));
  log('dev sigs', devSigs.length, 'oldest', devSigs.length && new Date(devSigs[devSigs.length - 1].blockTime * 1000).toISOString());

  // 2. dev txs, newest first, until we have enough creations
  const okSigs = devSigs.filter((s) => !s.err);
  const devTxs = [];
  const creations = [];
  const CHUNK = 100;
  for (let i = 0; i < okSigs.length; i += CHUNK) {
    const chunk = okSigs.slice(i, i + CHUNK);
    const txs = await pool(chunk, 10, (s) => getTx(s.signature));
    for (const [k, tx] of txs.entries()) {
      if (!tx) txs[k] = await getTx(chunk[k].signature); // one slow retry
    }
    txs.forEach((tx, k) => {
      if (!tx) {
        meta.errors.push(`dev tx missing ${chunk[k].signature}`);
        return;
      }
      devTxs.push(tx);
      let c = null;
      try {
        c = creationInfo(tx);
      } catch (e) {
        meta.errors.push(`creationInfo ${chunk[k].signature}: ${e.message}`);
      }
      if (c) creations.push(c);
    });
    log(`dev txs ${devTxs.length}/${okSigs.length} creations ${creations.length}`);
    if (creations.length >= cfg.maxTokens + 5 || Date.now() - T0 > BUDGET_MS * 0.35) break;
  }
  writeJsonlGz(path.join(OUT, 'dev_txs.jsonl.gz'), devTxs);
  fs.writeFileSync(path.join(OUT, 'creations.json'), JSON.stringify(creations, null, 1));

  // 3. per-mint history, newest token first
  const targets = creations.slice(0, cfg.maxTokens);
  meta.mints = [];
  for (const [n, c] of targets.entries()) {
    if (overBudget()) {
      meta.errors.push(`time budget hit after ${n} mints`);
      break;
    }
    const sigs = await allSigs(c.mint, cfg.maxMintSigs);
    // oldest first; keep the earliest maxMintTxs successful ones plus failed ones in that window
    const chron = [...sigs].reverse();
    const window = chron.slice(0, cfg.maxMintTxs);
    const txs = await pool(
      window.filter((s) => !s.err),
      10,
      (s) => getTx(s.signature),
    );
    const okWindow = window.filter((s) => !s.err);
    for (const [k, tx] of txs.entries()) {
      if (!tx) txs[k] = await getTx(okWindow[k].signature);
    }
    const got = txs.filter(Boolean);

    // tx ordering inside the first blocks: fetch block signature lists
    const blockIdx = {};
    const wanted = new Set(window.map((s) => s.signature));
    for (let slot = c.slot; slot <= c.slot + cfg.blockSlotsAfterCreate; slot++) {
      try {
        const b = await rpc(
          'getBlock',
          [slot, { transactionDetails: 'signatures', rewards: false, maxSupportedTransactionVersion: 1, commitment: 'confirmed' }],
          { tries: 4 },
        );
        if (!b) continue;
        const idx = {};
        b.signatures.forEach((s, i) => {
          if (wanted.has(s)) idx[s] = i;
        });
        blockIdx[slot] = { n: b.signatures.length, blockTime: b.blockTime, idx };
      } catch (e) {
        meta.errors.push(`getBlock ${slot}: ${e.message}`);
      }
    }

    fs.writeFileSync(
      path.join(OUT, 'mints', `${c.mint}.sigs.json`),
      JSON.stringify({ creation: c, totalSigs: sigs.length, truncated: sigs.length >= cfg.maxMintSigs, sigs: chron, blockIdx }),
    );
    writeJsonlGz(path.join(OUT, 'mints', `${c.mint}.txs.jsonl.gz`), got);

    let pfTrades = null;
    if (pf.status === 200) {
      pfTrades = await pumpApi(`https://frontend-api-v3.pump.fun/trades/all/${c.mint}?limit=200&offset=0&minimumSize=0`);
      fs.writeFileSync(path.join(OUT, 'mints', `${c.mint}.pumpfun_trades.json`), JSON.stringify(pfTrades));
    }
    meta.mints.push({ mint: c.mint, sigs: sigs.length, txs: got.length, missing: window.filter((s) => !s.err).length - got.length });
    log(`mint ${n + 1}/${targets.length} ${c.mint} sigs=${sigs.length} txs=${got.length}`);
    fs.writeFileSync(path.join(OUT, 'meta.json'), JSON.stringify({ ...meta, errLog }, null, 1));
  }

  meta.finishedAt = new Date().toISOString();
  meta.endpointStats = endpoints.map((e) => ({ url: e.url, ok: e.ok, err: e.err, okBy: e.okBy, hardErr: e.hardErr, bad: [...e.bad], interval: e.interval }));
  meta.errLog = errLog;
  fs.writeFileSync(path.join(OUT, 'meta.json'), JSON.stringify(meta, null, 1));
  log('done', meta.endpointStats);
}

main().catch((e) => {
  console.error(e, errLog);
  fs.writeFileSync(path.join(OUT, 'fatal.txt'), String(e.stack || e));
  process.exit(1);
});
