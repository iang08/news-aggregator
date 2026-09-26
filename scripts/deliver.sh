#!/bin/bash
# Deliver generated briefs from EVO-X2 to the Mac's Obsidian vault, over
# SSH/Tailscale. Runs on EVO-X2.
#
# Why a staging hop instead of rsync straight into the vault: macOS TCC blocks
# SSH-spawned processes from writing ~/Documents. So EVO-X2 rsyncs into a
# non-protected dir on the Mac (~/news_agg_inbox), and a Mac-side launchd agent
# (com.iangarlington.newsbrief.mover) moves the file into the vault — the Mac's
# own launchd CAN write ~/Documents.
#
# Idempotent + self-healing: once a brief reaches the Mac it's moved to
# delivered/ here, so this is safe to run on a short interval. That covers the
# "Mac was asleep at 7am" case — EVO-X2 (always on) keeps the brief and retries
# delivery until the Mac is reachable. Failure notes (*-brief-FAILED.md) travel
# the same way.
#
# After delivering a brief it also pulls ~/news_agg_feedback/ (one JSON per
# brief date with the picks Ian ticked, written by the Mac mover) into
# ~/news_agg_out/feedback/.
set -uo pipefail

OUT="$HOME/news_agg_out/00-Inbox"
DONE="$HOME/news_agg_out/delivered"
FEEDBACK="$HOME/news_agg_out/feedback"
MAC="zen@zens-macbook"
MAC_DEST="$MAC:news_agg_inbox/"
# Keepalives + rsync --timeout: a Mac that falls asleep mid-transfer must not
# leave this hanging (and holding the lock) for the kernel's TCP timeout.
SSH="ssh -o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

# One delivery at a time: the */15 cron and run_and_deliver.sh can overlap. Wait
# rather than skip, so a brief written just now isn't left for the next tick.
STATE="$HOME/.local/state/news_agg"
mkdir -p "$STATE" "$DONE"
exec 8>"$STATE/deliver.lock"
flock -w 300 8 || { echo "$(date '+%F %T') deliver: lock busy for 5 min; skipping this tick"; exit 0; }

cd "$OUT" 2>/dev/null || exit 0

shopt -s nullglob
delivered_brief=0
# rsync's last stderr line is its generic "rsync error: ... (code 255)"; the
# cause (ssh: connect ... timed out, Permission denied, ...) comes before it.
cause() { local m; m=$(printf '%s\n' "$1" | grep -v '^rsync error:' | grep -v '^$' | head -1)
    [ -n "$m" ] || m=$(printf '%s\n' "$1" | tail -1); printf '%s' "${m:0:160}"; }

for f in *-brief.md *-brief-FAILED.md; do
    if err=$(rsync -az --timeout=60 -e "$SSH" "$f" "$MAC_DEST" 2>&1); then
        mv -f "$f" "$DONE/"
        echo "$(date '+%F %T') delivered $f"
        case "$f" in *-brief.md) delivered_brief=1 ;; esac
    else
        echo "$(date '+%F %T') deliver FAILED for $f ($(cause "$err")); will retry"
    fi
done

# Once per delivered brief (not every 15 min — the laptop is often asleep).
# One file per brief date; no --delete, so EVO-X2's copy keeps accumulating
# after the Mac stops rewriting old dates.
if [ "$delivered_brief" = 1 ]; then
    mkdir -p "$FEEDBACK"
    if err=$(rsync -az --timeout=60 -e "$SSH" "$MAC:news_agg_feedback/" "$FEEDBACK/" 2>&1); then
        echo "$(date '+%F %T') pulled feedback ($(ls "$FEEDBACK" | wc -l) dates)"
    else
        echo "$(date '+%F %T') feedback pull failed ($(cause "$err"))"
    fi
fi
