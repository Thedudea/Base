# devsnipe

Analysis of whether copying two pump.fun dev wallets with GMGN "Dev Snipe" can be profitable.
The findings (in Persian) are in [REPORT.md](REPORT.md).

- `fetch/`: on-chain fetcher (Node 22, no deps), run by `.github/workflows/devsnipe-fetch.yml`
  because the dev sandbox has no Solana egress. Config: `fetch/config.json`.
- `data/<runTag>/<dev>/`: raw RPC output (dev txs, per-token signatures and transactions) plus
  analysis outputs (`analysis.json`, `sim_results.json`, `sensitivity.json`).
- `analysis/parse.py`: decodes pump.fun `TradeEvent`s into an exactly ordered trade tape with
  bonding-curve reserves before and after every trade.
- `analysis/analyze.py`: dev behaviour, bundled snipers, early buyers, perfect-exit upper bound.
- `analysis/simulate.py`: replay simulator. It inserts a GMGN buy after the dev's bundle at a
  sampled slot, replays later trades against the modified curve, and fires TP/SL with a sampled
  reaction delay. Costs include priority fee, tip, GMGN fee, pump fees, rent and failed txs.
- `analysis/run_sim.py`, `analysis/sensitivity.py`: parameter sweeps.
