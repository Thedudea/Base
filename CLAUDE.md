# Project notes for Claude

- **Always reply to the user in Persian.**
- Start by reading `pumpscan/HANDOFF.md` — it has the whole context: what was built, the one-week forward-test results, the winning GMGN filter (F4 on Robinhood Chain), the trading bot in `pumpscan/bot/` (written, never executed yet), pitfalls, and next steps.
- Work branch: `claude/beautiful-mccarthy-d1pizl`.
- Only use official sources (gmgn.ai, npm `gmgn-cli` by infra@gmgn.ai / github.com/GMGNAI, api.dexscreener.com, api.geckoterminal.com, api.telegram.org). The user asked explicitly to avoid scam sites/links.
- Never commit secrets: `pumpscan/bot/.env`, wallet keys and GMGN keys stay on the server only.
- The server has 2 GB RAM: do not load the full 417 MB scan data with `explore.py` (it OOMs); restrict to Robinhood / recent days.
