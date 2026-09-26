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
  2. if it's past STALE_AFTER (Pacific), the Mac has been awake long enough for
     a delivery to have happened, and today's brief hasn't arrived, pops a
     "missing" notification once — the case where EVO-X2 itself is down and
     can't report anything (2026-09-25: down 03:26-12:25, silence all day)
  3. writes the checkboxes Ian ticked in recent briefs to
     ~/news_agg_feedback/<date>.json, which EVO-X2's deliver.sh pulls
A file that can't be moved into the vault pops a "stuck" notification.
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
FEEDBACK_DIR = os.path.expanduser("~/news_agg_feedback")

# Briefs are dated and generated in Pacific time on EVO-X2, wherever the laptop is.
PT = ZoneInfo("America/Los_Angeles")
STALE_AFTER = dtime(9, 30)  # 07:00 run + catch-up slack
# After a wake, EVO-X2's */15 deliver.sh needs time to reach the Mac (Tailscale
# reconnect + up to two ticks) — without this, waking after 09:30 alarmed ~1 day in 4.
AWAKE_BEFORE_ALARM = timedelta(minutes=35)
SLEEP_GAP = timedelta(minutes=10)  # launchd runs us every 120 s; a longer gap = asleep
FEEDBACK_DAYS = 30

_PICK_RE = re.compile(r"^- \[([ xX])\] \*\*\[(.+?)\]\((https?://[^)\s]+)\)\*\*")
_BRIEF_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-brief\.md$")


def logline(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def notify(title: str, message: str) -> bool:
    """macOS notification. The text goes in as argv, never into the script
    source: quoting it into AppleScript broke on the em dash in half the
    messages, and the failure was silent. Returns True if osascript succeeded."""
    cmd = ["/usr/bin/osascript",
           "-e", "on run argv",
           "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
           "-e", "end run",
           message[:220], title]
    try:
        r = subprocess.run(cmd, timeout=15, capture_output=True, text=True)
    except Exception as e:  # noqa: BLE001 — a notification must never break the move
        logline(f"notification FAILED ({e}): {title}: {message[:120]}")
        return False
    if r.returncode != 0:
        logline(f"notification FAILED (rc={r.returncode} {r.stderr.strip()[:200]}): {title}: {message[:120]}")
        return False
    logline(f"notified: {title}: {message[:120]}")
    return True


def notify_once(key: str, title: str, message: str) -> None:
    """Notify at most once per key; the marker is written only after success."""
    os.makedirs(STATE, exist_ok=True)
    marker = os.path.join(STATE, f"alerted-{key}")
    if not os.path.exists(marker) and notify(title, message):
        open(marker, "w").close()


def awake_since(now: datetime) -> datetime:
    """When the Mac last woke (or the mover first ran), tracked across passes."""
    os.makedirs(STATE, exist_ok=True)
    last_pass_f = os.path.join(STATE, "last-pass")
    awake_f = os.path.join(STATE, "awake-since")
    try:
        last = datetime.fromtimestamp(float(open(last_pass_f).read()), now.tzinfo)
        since = datetime.fromtimestamp(float(open(awake_f).read()), now.tzinfo)
    except (OSError, ValueError):
        last = since = None
    if last is None or since is None or now - last > SLEEP_GAP:
        since = now
        with open(awake_f, "w") as f:
            f.write(str(now.timestamp()))
    with open(last_pass_f, "w") as f:
        f.write(str(now.timestamp()))
    return since


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
                extra = alert_for(src)
                notify_once(f"stuck-{name}", "News brief stuck",
                            f"{name} arrived but can't be moved into the vault: {last_err}"
                            + (f" ({extra[0]}: {extra[1]})" if extra else ""))
                continue
            alert = alert_for(dest)
            if alert:
                notify(*alert)


def check_missing(now_pt: datetime, since: datetime) -> None:
    """Notify once a day if today's brief (or a failure note) hasn't arrived.
    A file stuck in the inbox counts as arrived — move_new_files alerts on that."""
    if now_pt.time() < STALE_AFTER or now_pt - since < AWAKE_BEFORE_ALARM:
        return
    today = now_pt.strftime("%Y-%m-%d")
    names = (f"{today}-brief.md", f"{today}-brief-FAILED.md")
    if any(os.path.exists(os.path.join(d, n)) for d in (VAULT, INBOX) for n in names):
        return
    notify_once(f"missing-{today}", "News brief missing",
                f"No brief for {today} yet — EVO-X2 may be down, or the 07:00 run is late.")


def harvest_feedback(now_pt: datetime) -> None:
    """Write every pick checkbox of the last FEEDBACK_DAYS of briefs to
    FEEDBACK_DIR/<date>.json (only when it changed). One file per date, so an
    unreadable vault or an aged-out brief never erases what was harvested."""
    cutoff = (now_pt - timedelta(days=FEEDBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        os.listdir(VAULT)
    except OSError as e:
        logline(f"feedback: vault unlistable ({e}); keeping previous files")
        return
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
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    for date, picks in briefs.items():
        path = os.path.join(FEEDBACK_DIR, f"{date}.json")
        snapshot = json.dumps({"date": date, "picks": picks}, ensure_ascii=False, indent=1)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == snapshot:
                    continue
        except OSError:
            pass
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(snapshot)
        os.replace(path + ".tmp", path)
        logline(f"feedback: {date}: {sum(p['checked'] for p in picks)}/{len(picks)} picks ticked")


def main() -> int:
    now_pt = datetime.now(PT)
    since = now_pt
    try:
        since = awake_since(now_pt)
    except Exception as e:  # noqa: BLE001
        logline(f"awake tracking failed: {e}")
    for step in (move_new_files, lambda: check_missing(now_pt, since), lambda: harvest_feedback(now_pt)):
        try:
            step()
        except Exception as e:  # noqa: BLE001 — each step is independent
            logline(f"step failed: {e}")
    return 0  # job did its work; per-file failures are logged + retried


if __name__ == "__main__":
    raise SystemExit(main())
