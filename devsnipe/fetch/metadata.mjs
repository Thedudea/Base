// Fetches the off-chain metadata JSON of every token a dev created (the URI is
// inside the pump.fun CreateEvent / LaunchLab logs) to see what each launch copies.
//   node metadata.mjs <dataDir>/<dev>
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const dir = process.argv[2];
const txs = zlib.gunzipSync(fs.readFileSync(path.join(dir, 'dev_txs.jsonl.gz'))).toString().split('\n').filter(Boolean).map(JSON.parse);
const uris = new Map();
for (const tx of txs) {
  if (!tx.meta || tx.meta.err) continue;
  for (const l of tx.meta.logMessages || []) {
    if (!l.startsWith('Program data: ')) continue;
    const s = Buffer.from(l.slice(14), 'base64').toString('latin1');
    for (const m of s.matchAll(/https?:\/\/[\x21-\x7e]+/g)) uris.set(m[0], tx.blockTime);
  }
}
const out = [];
for (const [uri, t] of uris) {
  try {
    const r = await fetch(uri, { signal: AbortSignal.timeout(15000) });
    const body = await r.text();
    let json = null;
    try { json = JSON.parse(body); } catch {}
    out.push({ uri, blockTime: t, status: r.status, json, raw: json ? undefined : body.slice(0, 300) });
  } catch (e) {
    out.push({ uri, blockTime: t, error: e.message });
  }
}
fs.writeFileSync(path.join(dir, 'metadata.json'), JSON.stringify(out, null, 1));
console.log('uris', uris.size, 'ok', out.filter((o) => o.json).length);
