#!/usr/bin/env python3
"""Move briefs EVO-X2 dropped into the staging dir into the Obsidian vault,
and make trouble loud.

Run by the project venv Python on purpose: that binary
(/Users/zen/projects/news_agg/.venv/bin/python) already holds a persistent
macOS TCC grant to write ~/Documents (it wrote briefs there for weeks under the
old on-Mac job). A /bin/bash launchd agent does NOT have that grant and gets
denied — see git history. Runs on a launchd interval; sleep-tolerant.

Each pass:
  1. moves *-brief.md and *-brief-FAILED.md into the vault; a failure note or a
     brief marked `status: degraded` pops a macOS notification
  2. if it's past STALE_AFTER (Pacific) and today's brief hasn't arrived, pops a
     "missing" notification once — the case where EVO-X2 itself is down and
     can't report anything (2026-09-25: down 03:26-12:25, silence all day)
  3. harvests the checkboxes Ian ticked in recent briefs into
     ~/news_agg_feedback/ticks.json, which EVO-X2's deliver.sh pulls
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

INBOX = os.path.expanduser("~/news_agg_inbox")
VAULT = os.path.expanduser("~/Documents/obsidian/myvault/00-Inbox")
LOG = os.path.expanduser("~/Library/Logs/news_agg_mover.log")
STATE = os.path.expanduser("~/Library/Application Support/news_agg")
FEEDBACK = os.path.expanduser("~/news_agg_feedback/ticks.json")

# Briefs are dated and generated in Pacific time on EVO-X2, wherever the laptop is.
PT = ZoneInfo("America/Los_Angeles")
STALE_AFTER = dtime(9, 30)  # 07:00 run + catch-up slack
FEEDBACK_DAYS = 30

_PICK_RE = re.compile(r"^- \[([ xX])\] \*\*\[(.+?)\]\((https?://[^)\s]+)\)\*\*")
_BRIEF_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-brief\.md$")


def logline(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def notify(title: str, message: str) -> None:
    """macOS notification. json.dumps yields a valid AppleScript string literal."""
    script = f"display notification {json.dumps(message[:220])} with title {json.dumps(title)}"
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=15, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logline(f"notified: {title}: {message[:120]}")
    except Exception as e:  # noqa: BLE001 — a notification must never break the move
        logline(f"notification failed ({e}): {title}: {message[:120]}")


def _plain(s: str) -> str:
    return re.sub(r"[*`>]|⚠️", "", s).strip()


def alert_for(path: str) -> tuple[str, str] | None:
    """(title, message) if this delivered file needs Ian's attention."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    name = os.path.basename(path)
    if name.endswith("-brief-FAILED.md"):
        m = re.search(r"^\*\*Reason:\*\*\s*(.+)$", text, re.M)
        return "News brief FAILED", _plain(m.group(1)) if m else name
    if re.search(r"^status: degraded$", text, re.M):
        first = next((line for line in text.splitlines() if line.startswith("> ⚠️")), "")
        return "News brief degraded", _plain(first) or name
    return None


def move_new_files() -> None:
    os.makedirs(VAULT, exist_ok=True)
    for pattern in ("*-brief.md", "*-brief-FAILED.md"):
        for src in glob.glob(os.path.join(INBOX, pattern)):
            name = os.path.basename(src)
            dest = os.path.join(VAULT, name)
            last_err = None
            for _ in range(3):
                try:
                    shutil.move(src, dest)
                    logline(f"moved {name} -> vault")
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001 — log + retry next interval
                    last_err = e
                    time.sleep(2)
            if last_err is not None:
                logline(f"FAILED to move {name}: {last_err}")
                continue
            alert = alert_for(dest)
            if alert:
                notify(*alert)


def check_missing(now_pt: datetime) -> None:
    """Notify once a day if today's brief (or a failure note) hasn't arrived."""
    if now_pt.time() < STALE_AFTER:
        return
    today = now_pt.strftime("%Y-%m-%d")
    names = (f"{today}-brief.md", f"{today}-brief-FAILED.md")
    if any(os.path.exists(os.path.join(d, n)) for d in (VAULT, INBOX) for n in names):
        return
    os.makedirs(STATE, exist_ok=True)
    marker = os.path.join(STATE, f"missing-alerted-{today}")
    if os.path.exists(marker):
        return
    open(marker, "w").close()
    for old in glob.glob(os.path.join(STATE, "missing-alerted-*")):
        if old != marker:
            os.remove(old)
    notify("News brief missing", f"No brief for {today} yet — EVO-X2 may be down, or the 07:00 run is late.")


def harvest_feedback(now_pt: datetime) -> None:
    """Snapshot every pick checkbox in the last FEEDBACK_DAYS of briefs."""
    cutoff = (now_pt - timedelta(days=FEEDBACK_DAYS)).strftime("%Y-%m-%d")
    briefs: dict[str, list[dict]] = {}
    for path in sorted(glob.glob(os.path.join(VAULT, "*-brief.md"))):
        m = _BRIEF_RE.match(os.path.basename(path))
        if not m or m.group(1) < cutoff:
            continue
        picks = []
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    pm = _PICK_RE.match(line)
                    if pm:
                        picks.append({"title": pm.group(2), "url": pm.group(3),
                                      "checked": pm.group(1) != " "})
        except OSError:
            continue
        if picks:  # briefs from before checkboxes existed have none
            briefs[m.group(1)] = picks
    snapshot = json.dumps({"briefs": briefs}, ensure_ascii=False, indent=1, sort_keys=True)
    try:
        with open(FEEDBACK, encoding="utf-8") as f:
            if f.read() == snapshot:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(FEEDBACK), exist_ok=True)
    tmp = FEEDBACK + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(snapshot)
    os.replace(tmp, FEEDBACK)
    ticked = sum(p["checked"] for ps in briefs.values() for p in ps)
    logline(f"feedback: {ticked} ticked picks across {len(briefs)} briefs")


def main() -> int:
    now_pt = datetime.now(PT)
    for step in (move_new_files, lambda: check_missing(now_pt), lambda: harvest_feedback(now_pt)):
        try:
            step()
        except Exception as e:  # noqa: BLE001 — each step is independent
            logline(f"step failed: {e}")
    return 0  # job did its work; per-file failures are logged + retried


if __name__ == "__main__":
    raise SystemExit(main())
