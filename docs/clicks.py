"""Which brief picks did Ian actually open? A one-off, read-only check.

Run it yourself, on a COPY of Chrome's history (close Chrome first):
    cp ~/Library/Application\\ Support/Google/Chrome/Default/History /tmp/History.copy
    python3 docs/clicks.py /tmp/History.copy [vault_inbox_dir]
    rm /tmp/History.copy

Parses every *-brief.md in the vault inbox (old and checkbox formats), then
counts visits to each pick's URL within 48 h after the brief was generated.
Prints aggregate open rates by source, category, score and position only —
nothing else from the history is read out.
"""
import collections
import glob
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

PICK = re.compile(r"^- (?:\[[ xX]\] )?\*\*\[(.*)\]\((\S+)\)\*\*")
SCORE = re.compile(r"\*(.+?)\*.*score (\d+)/10")
GEN = re.compile(r"\*Generated (\d{4}-\d\d-\d\d \d\d:\d\d)")
EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)  # Chrome's visit_time epoch


def picks(inbox: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(os.path.join(inbox, "*-brief.md"))):
        text = open(f, encoding="utf-8", errors="ignore").read()
        g = GEN.search(text)
        if not g:
            continue
        gen = datetime.strptime(g.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        cat, pos, lines = None, 0, text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("## "):
                cat = line[3:].strip().lower()
            m = PICK.match(line)
            if m:
                pos += 1
                s = SCORE.search(lines[i + 1]) if i + 1 < len(lines) else None
                rows.append({"gen": gen, "cat": cat, "pos": pos, "url": m.group(2),
                             "src": s.group(1) if s else "?", "score": int(s.group(2)) if s else None})
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = sys.argv[1]
    if "Application Support" in src:
        sys.exit("pass a COPY of the History file, not the live one")
    inbox = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/Documents/obsidian/myvault/00-Inbox")
    strip = lambda u: urlunsplit(urlsplit(u)._replace(query="", fragment="")).rstrip("/")
    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    visits = collections.defaultdict(list)
    for url, vt, fv in db.execute("select u.url, v.visit_time, v.from_visit from visits v join urls u on u.id = v.url"):
        e = (EPOCH + timedelta(microseconds=vt), fv)
        visits[url].append(e)
        visits["S:" + strip(url)].append(e)
    if not visits:
        sys.exit("no visits in this history copy")
    oldest = min(t for v in visits.values() for t, _ in v)
    rows = []
    for p in picks(inbox):
        if p["gen"] < oldest:
            continue  # before Chrome's retained history
        vs = visits.get(p["url"]) or visits.get("S:" + strip(p["url"]), [])
        vs = [(t, fv) for t, fv in vs if p["gen"] <= t <= p["gen"] + timedelta(hours=48)]
        # from_visit == 0: opened from outside Chrome (e.g. a link in Obsidian)
        p["opened"] = any(fv == 0 for _, fv in vs)
        rows.append(p)
    print(f"picks since {oldest.date()}: {len(rows)} | opened from outside Chrome: {sum(p['opened'] for p in rows)}")
    for key in ("src", "cat", "score", "pos"):
        c = collections.defaultdict(lambda: [0, 0])
        for p in rows:
            c[p[key]][0] += 1
            c[p[key]][1] += p["opened"]
        print(f"\n== by {key}")
        for k, (n, o) in sorted(c.items(), key=lambda x: -x[1][0])[:15]:
            print(f"  {str(k)[:30]:30} n={n:4} opened={o:4} {o / n:5.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
