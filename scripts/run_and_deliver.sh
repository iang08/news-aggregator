#!/bin/bash
# EVO-X2 daily entrypoint (7am cron): generate the brief, beat the ops-dashboard
# heartbeat, then deliver to the Mac. Generation always runs on EVO-X2 (an
# always-on server) so a sleeping laptop can no longer cause a missed brief —
# the brief is produced regardless, and delivered whenever the Mac is reachable.
set -uo pipefail

cd "$HOME/projects/news_agg" || exit 1

# One pipeline at a time, however it was started (07:00 cron, catchup.sh, or by
# hand): two runs could both load the local fallback model on a shared box, and
# a second run overwrites today's brief. catchup.sh takes this lock itself and
# hands it down (NEWS_AGG_RUN_LOCK_HELD=1) so its check-then-run is atomic.
if [ -z "${NEWS_AGG_RUN_LOCK_HELD:-}" ]; then
    LOCK="$HOME/.local/state/news_agg/run.lock"
    mkdir -p "${LOCK%/*}"
    exec 9>"$LOCK"
    if ! flock -n 9; then
        echo "$(date '+%F %T') another news_agg run holds $LOCK; not starting a second" >&2
        exit 75
    fi
fi

PYTHONPATH=. .venv/bin/python -m aggregator.main
rc=$?

# NOTE: the ops-dashboard heartbeat is written by main.py itself
# (_write_heartbeat -> ~/.ops-heartbeats/newsbrief), so we don't beat here.
# Since main.py now runs on EVO-X2, that heartbeat lives on EVO-X2 — point the
# dashboard at EVO-X2's ~/.ops-heartbeats/newsbrief (same as us_sourcing).

# Deliver whatever is pending (this run's brief, plus any earlier undelivered)
"$HOME/projects/news_agg/scripts/deliver.sh"

exit "$rc"
