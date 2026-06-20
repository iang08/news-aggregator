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
# delivery until the Mac is reachable.
set -uo pipefail

OUT="$HOME/news_agg_out/00-Inbox"
DONE="$HOME/news_agg_out/delivered"
MAC_DEST="zen@zens-macbook:news_agg_inbox/"

mkdir -p "$DONE"
cd "$OUT" 2>/dev/null || exit 0

shopt -s nullglob
for f in *-brief.md; do
    if rsync -az -e "ssh -o ConnectTimeout=10 -o BatchMode=yes" "$f" "$MAC_DEST" 2>/dev/null; then
        mv -f "$f" "$DONE/"
        echo "$(date '+%F %T') delivered $f"
    else
        echo "$(date '+%F %T') deliver FAILED for $f (mac unreachable?); will retry"
    fi
done
