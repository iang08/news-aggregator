#!/bin/bash
# Catch up a missed 07:00 brief. Runs on EVO-X2 from a */15 cron line.
#
# Cron has no catch-up: on 2026-09-25 EVO-X2 crashed at 03:26 and came back at
# 12:25, the 07:00 job never fired, and there was no brief until one was run by
# hand at 21:56. This guard runs scripts/run_and_deliver.sh once when, inside
# the catch-up window, today's brief is in neither 00-Inbox/ nor delivered/.
# That covers a missed 07:00 slot (box down or asleep) and a 07:00 run that
# failed.
#
# Guards:
#   - One pipeline at a time: takes run_and_deliver.sh's lock BEFORE the final
#     brief check and holds it through the run, so check-then-run is atomic. A
#     second run would overwrite today's note in the vault.
#   - One catch-up attempt per day: the date is recorded before launching, so a
#     failing run is not retried every 15 minutes.
#   - Memory: with fewer than MIN_AVAIL_GB available it runs Claude-only
#     (TRIAGE_LOCAL_FALLBACK=0), since the local fallback loads
#     qwen3.6:35b-a3b (~23 GB) on a shared box. If Claude fails too, the
#     failure note says so.
#
# Dry-run by default: prints what it would do. Cron passes --apply.
set -uo pipefail

EARLIEST=0715     # the 07:00 job holds the lock while it runs; don't race its start
LATEST=2200       # no new catch-up at or after this time (HHMM, local)
MIN_AVAIL_GB=50   # fallback model ~25 GB loaded + ~25 GB headroom for the box

REPO="$HOME/projects/news_agg"
OUT="$HOME/news_agg_out"
STATE="$HOME/.local/state/news_agg"
LOCK="$STATE/run.lock"               # shared with run_and_deliver.sh
ATTEMPT="$STATE/catchup_last_attempt"
CLAUDE_ONLY="$STATE/catchup_claude_only_attempt"  # low-memory Claude-only try

APPLY=0
case "${1:-}" in
    --apply) APPLY=1 ;;
    "") ;;
    *) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

log() { echo "$(date '+%F %T') catchup: $*"; }
# Routine no-op reasons: silent under cron (96 ticks/day), shown in a dry-run.
note() { [ "$APPLY" = 1 ] || log "$*"; }

read -r today hhmm <<< "$(date '+%F %H%M')"
brief="$today-brief.md"

brief_exists() { [ -e "$OUT/00-Inbox/$brief" ] || [ -e "$OUT/delivered/$brief" ]; }

if brief_exists; then
    note "$brief exists; nothing to do"; exit 0
fi
if [ "$hhmm" -lt "$EARLIEST" ] || [ "$hhmm" -ge "$LATEST" ]; then
    note "no $brief, but $hhmm is outside the catch-up window $EARLIEST-$LATEST"; exit 0
fi
if [ "$(cat "$ATTEMPT" 2>/dev/null)" = "$today" ]; then
    note "no $brief, but today's catch-up was already attempted; not retrying"; exit 0
fi

mkdir -p "$STATE"
exec 9>"$LOCK"
if ! flock -n 9; then
    log "no $brief yet, but a pipeline run holds $LOCK; re-checking next tick"; exit 0
fi
if brief_exists; then  # the run that just released the lock may have written it
    note "$brief appeared while waiting for the lock; nothing to do"; exit 0
fi

avail=$(free -g | awk '/^Mem:/ {print $7}')
marker="$ATTEMPT"
mode="with the local fallback"
if [ "${avail:-0}" -lt "$MIN_AVAIL_GB" ]; then
    # Too tight to load the local model, but Claude needs no memory here: try
    # Claude-only ONCE (its own marker) and keep the full attempt for when
    # memory frees. Deferring was silent and could last all day, and spending
    # the only attempt Claude-only on the same bad-API morning left no retry
    # with the local model once the box freed up.
    if [ "$(cat "$CLAUDE_ONLY" 2>/dev/null)" = "$today" ]; then
        log "no $brief; Claude-only catch-up already tried and only ${avail:-?} GB available (need $MIN_AVAIL_GB); deferring the full attempt"
        exit 0
    fi
    export TRIAGE_LOCAL_FALLBACK=0
    marker="$CLAUDE_ONLY"
    mode="Claude-only (only ${avail:-?} GB available, the local fallback needs $MIN_AVAIL_GB)"
fi

if [ "$APPLY" != 1 ]; then
    log "DRY RUN: would run run_and_deliver.sh now, $mode (no $brief at $hhmm)"
    exit 0
fi

echo "$today" > "$marker"
log "no $brief at $hhmm; running run_and_deliver.sh $mode, output in cron.log"
NEWS_AGG_RUN_LOCK_HELD=1 "$REPO/scripts/run_and_deliver.sh" >> "$REPO/cron.log" 2>&1
rc=$?
if brief_exists; then
    log "catch-up done (rc=$rc): $brief written"
else
    if [ "$marker" = "$ATTEMPT" ]; then log "catch-up FAILED (rc=$rc): no $brief; no more attempts today, see cron.log"
    else log "Claude-only catch-up FAILED (rc=$rc): no $brief; the full attempt waits for $MIN_AVAIL_GB GB, see cron.log"; fi
fi
exit "$rc"
