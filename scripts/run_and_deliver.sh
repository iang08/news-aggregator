#!/bin/bash
# EVO-X2 daily entrypoint (7am cron): generate the brief, beat the ops-dashboard
# heartbeat, then deliver to the Mac. Generation always runs on EVO-X2 (an
# always-on server) so a sleeping laptop can no longer cause a missed brief —
# the brief is produced regardless, and delivered whenever the Mac is reachable.
set -uo pipefail

cd "$HOME/projects/news_agg" || exit 1

PYTHONPATH=. .venv/bin/python -m aggregator.main
rc=$?

# ops-dashboard heartbeat (ok|fail by exit code) — same convention as us_sourcing
"$HOME/.ops-heartbeats/beat.sh" news_brief "$rc" 2>/dev/null || true

# Deliver whatever is pending (this run's brief, plus any earlier undelivered)
"$HOME/projects/news_agg/scripts/deliver.sh"

exit "$rc"
