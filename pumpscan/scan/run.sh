#!/usr/bin/env bash
# GitHub Actions driver for the scanner: runs it for one job's worth of time,
# commits the snapshots every 30 minutes, then dispatches the next run so the
# scan continues until config.until (or until config.enabled is false).
set -u
cfg=pumpscan/scan/config.json
out=pumpscan/data/scan
field() { node -p "require('./$cfg').$1"; }

if [ "$(field enabled)" != "true" ] || [ "$(date -u +%s)" -ge "$(date -u -d "$(field until)" +%s)" ]; then
  echo "scanner disabled or past 'until'"; exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

commit() {
  git add "$out"
  git commit -q -m "pumpscan: scan snapshots $(date -u +%FT%H:%MZ)" || return 0
  for i in 1 2 3 4; do
    git pull -q --rebase --autostash origin "$GITHUB_REF_NAME" && git push -q origin "HEAD:$GITHUB_REF_NAME" && return 0
    sleep $((5 * i))
  done
}

node pumpscan/scan/scanner.mjs "$out" &
pid=$!
while kill -0 "$pid" 2>/dev/null; do
  # wake just after each :00 / :30 so the half-hour file being committed is complete
  s=$(( 1800 - $(date +%s) % 1800 + 45 ))
  for _ in $(seq "$s"); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  commit
done
wait "$pid"; echo "scanner exit $?"
commit

# re-read config from the branch (it may have been turned off meanwhile) and chain the next run
git pull -q --rebase --autostash origin "$GITHUB_REF_NAME" || true
if [ "$(field enabled)" = "true" ] && [ "$(date -u +%s)" -lt "$(date -u -d "$(field until)" +%s)" ]; then
  gh workflow run pumpscan-scan.yml --ref "$GITHUB_REF_NAME" && echo "next run dispatched"
fi
