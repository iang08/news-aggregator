#!/usr/bin/env python3
"""Move briefs EVO-X2 dropped into the staging dir into the Obsidian vault.

Run by the project venv Python on purpose: that binary
(/Users/zen/projects/news_agg/.venv/bin/python) already holds a persistent
macOS TCC grant to write ~/Documents (it wrote briefs there for weeks under the
old on-Mac job). A /bin/bash launchd agent does NOT have that grant and gets
denied — see git history. Runs on a launchd interval; sleep-tolerant.
"""
import glob
import os
import shutil
import time

INBOX = os.path.expanduser("~/news_agg_inbox")
VAULT = os.path.expanduser("~/Documents/obsidian/myvault/00-Inbox")
LOG = os.path.expanduser("~/Library/Logs/news_agg_mover.log")


def logline(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def main() -> int:
    os.makedirs(VAULT, exist_ok=True)
    for src in glob.glob(os.path.join(INBOX, "*-brief.md")):
        name = os.path.basename(src)
        last_err = None
        for _ in range(3):
            try:
                shutil.move(src, os.path.join(VAULT, name))
                logline(f"moved {name} -> vault")
                last_err = None
                break
            except Exception as e:  # noqa: BLE001 — log + retry next interval
                last_err = e
                time.sleep(2)
        if last_err is not None:
            logline(f"FAILED to move {name}: {last_err}")
    return 0  # job did its work; per-file failures are logged + retried


if __name__ == "__main__":
    raise SystemExit(main())
