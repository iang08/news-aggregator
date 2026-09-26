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
#   - Memory: defers (without spending the attempt) while fewer than
#     MIN_AVAIL_GB are available, since the local fallback loads
#     qwen3.6:35b-a3b (~23 GB) on a shared box.
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
if [ "${avail:-0}" -lt "$MIN_AVAIL_GB" ]; then
    log "no $brief, but only ${avail:-?} GB available (need $MIN_AVAIL_GB); deferring to next tick"
    exit 0
fi

if [ "$APPLY" != 1 ]; then
    log "DRY RUN: would run run_and_deliver.sh now (no $brief at $hhmm, ${avail} GB available)"
    exit 0
fi

echo "$today" > "$ATTEMPT"
log "no $brief at $hhmm (${avail} GB available); running run_and_deliver.sh, output in cron.log"
NEWS_AGG_RUN_LOCK_HELD=1 "$REPO/scripts/run_and_deliver.sh" >> "$REPO/cron.log" 2>&1
rc=$?
if brief_exists; then
    log "catch-up done (rc=$rc): $brief written"
else
    log "catch-up FAILED (rc=$rc): no $brief; no more attempts today, see cron.log"
fi
exit "$rc"
