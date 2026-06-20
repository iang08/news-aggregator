#!/bin/bash
# EVO-X2 daily entrypoint (7am cron): generate the brief, beat the ops-dashboard
# heartbeat, then deliver to the Mac. Generation always runs on EVO-X2 (an
# always-on server) so a sleeping laptop can no longer cause a missed brief —
# the brief is produced regardless, and delivered whenever the Mac is reachable.
set -uo pipefail

cd "$HOME/projects/news_agg" || exit 1

PYTHONPATH=. .venv/bin/python -m aggregator.main
rc=$?

# NOTE: the ops-dashboard heartbeat is written by main.py itself
# (_write_heartbeat -> ~/.ops-heartbeats/newsbrief), so we don't beat here.
# Since main.py now runs on EVO-X2, that heartbeat lives on EVO-X2 — point the
# dashboard at EVO-X2's ~/.ops-heartbeats/newsbrief (same as us_sourcing).

# Deliver whatever is pending (this run's brief, plus any earlier undelivered)
"$HOME/projects/news_agg/scripts/deliver.sh"

exit "$rc"
