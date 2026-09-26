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

# Hard limit on the whole pipeline. A normal run takes ~1 min and the local
# fallback ~5; anything near 45 min is hung — and while it hangs it holds the
# lock every catch-up needs. fetch.py and triage.py have their own per-call
# limits; this is the backstop for whatever slips past them.
RUN_LIMIT=45m
today=$(date '+%F')
timeout --kill-after=60 "$RUN_LIMIT" env PYTHONPATH=. NEWS_AGG_HEARTBEAT=1 .venv/bin/python -m aggregator.main
rc=$?

# A killed or crashed run can't report itself (main.py writes the failure note
# and the heartbeat on its own failures), so do it here: the failure must reach
# the inbox, not only this log. rc 3 = today's brief already existed.
out="$HOME/news_agg_out"
reported() { for d in "$out/00-Inbox" "$out/delivered"; do
    [ -e "$d/$today-brief.md" ] || [ -e "$d/$today-brief-FAILED.md" ] && return 0; done; return 1; }
if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ] && { [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] || ! reported; }; then
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        reason="the run was killed after $RUN_LIMIT (a hung feed or model call)"
    else
        reason="aggregator.main exited $rc without reporting (see cron.log)"
    fi
    echo "$(date '+%F %T') | ERROR | $reason"
    inbox="$out/00-Inbox"
    if [ ! -e "$inbox/$today-brief.md" ]; then
        mkdir -p "$inbox"
        cat > "$inbox/$today-brief-FAILED.md" <<EOF
---
type: news-brief-failure
date: $today
status: fail
---
# ⚠️ News brief FAILED — $today

The run on EVO-X2 at $(date '+%H:%M') did not produce a brief.

**Reason:** $reason

Next: if this was the 07:00 run, the catch-up tries once more between 07:15 and 22:00. Details are in cron.log on EVO-X2 (~/projects/news_agg).
EOF
    fi
    mkdir -p "$HOME/.ops-heartbeats"
    echo "$(date +%s) fail rc=$rc" > "$HOME/.ops-heartbeats/newsbrief"
fi

# NOTE: the ops-dashboard heartbeat is written by main.py itself
# (_write_heartbeat -> ~/.ops-heartbeats/newsbrief), so we don't beat here.
# Since main.py now runs on EVO-X2, that heartbeat lives on EVO-X2 — point the
# dashboard at EVO-X2's ~/.ops-heartbeats/newsbrief (same as us_sourcing).

# Deliver whatever is pending (this run's brief, plus any earlier undelivered)
"$HOME/projects/news_agg/scripts/deliver.sh"

exit "$rc"
