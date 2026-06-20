#!/bin/bash
# Move briefs that EVO-X2 dropped into the staging dir into the Obsidian vault.
# Runs on a launchd interval (sleep-tolerant: clears any backlog on wake).
# Retries within a run so a transient TCC/IO hiccup doesn't strand a brief.
set -uo pipefail
INBOX="$HOME/news_agg_inbox"
VAULT="$HOME/Documents/obsidian/myvault/00-Inbox"
LOG="$HOME/Library/Logs/news_agg_mover.log"   # outside INBOX
mkdir -p "$VAULT"
shopt -s nullglob
for f in "$INBOX"/*-brief.md; do
  moved=0
  for try in 1 2 3; do
    if mv -f "$f" "$VAULT/" 2>/dev/null; then
      echo "$(date '+%F %T') moved $(basename "$f") -> vault" >> "$LOG"; moved=1; break
    fi
    sleep 2
  done
  [ "$moved" = 0 ] && echo "$(date '+%F %T') FAILED to move $(basename "$f") after 3 tries" >> "$LOG"
done
exit 0   # job did its work; per-file failures are logged + retried next interval
