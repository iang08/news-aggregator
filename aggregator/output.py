"""
output.py — Writes the triage result to a markdown file in Obsidian vault.

Format: simple navigable list. User clicks links, reads in browser,
highlights/saves via Web Clipper. No pre-synthesis here. Each pick is a
checkbox: ticking the ones that were worth reading is the feedback signal
(harvested on the Mac by scripts/mac/news_agg_move_brief.py).

A run that fails writes YYYY-MM-DD-brief-FAILED.md to the same inbox instead,
so the failure lands where Ian reads, not only in a log on EVO-X2.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from aggregator.fetch import SourceStatus
from aggregator.triage import TriageResult, _history_dirs, load_env

logger = logging.getLogger(__name__)

# Section order. Unknown categories fall to the end; world news last on
# purpose (Ian wants less geopolitics).
CATEGORY_ORDER = ["ai", "tech", "japan", "cars", "science", "health", "philosophy", "ideas", "local", "world"]


def _inbox() -> Path:
    load_env()
    vault_path = os.getenv("OBSIDIAN_VAULT_PATH")
    inbox_folder = os.getenv("OBSIDIAN_BRIEF_FOLDER", "00-Inbox")

    if not vault_path:
        raise RuntimeError("OBSIDIAN_VAULT_PATH not set in .env")

    vault = Path(vault_path)
    if not vault.exists():
        raise FileNotFoundError(f"Obsidian vault not found at {vault_path}")

    inbox = vault / inbox_folder
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def existing_brief(date_str: str) -> Path | None:
    """Today's brief if it already exists — still in the output dir, or already
    delivered (a BRIEF_HISTORY_DIRS dir)."""
    name = f"{date_str}-brief.md"
    for p in [_inbox() / name, *(Path(d) / name for d in _history_dirs())]:
        if p.exists():
            return p
    return None


def failure_note_name(date_str: str) -> str:
    # Deliberately NOT "*-brief.md": the catch-up, cross-day dedup and the
    # delivered-brief checks all key on "<date>-brief.md" and must not mistake
    # a failure note for a brief.
    return f"{date_str}-brief-FAILED.md"


def write_brief(
    result: TriageResult,
    status: str = "ok",
    notices: list[str] | None = None,
    sources_down: list[SourceStatus] | None = None,
    sources_stale: list[SourceStatus] | None = None,
) -> Path:
    """Format the triage result as markdown and write to Obsidian vault.

    Refuses to replace a brief that already exists for today — in the output
    dir or already delivered — unless BRIEF_OVERWRITE=1: the delivered copy is
    in Ian's vault and may carry his ticks."""
    inbox = _inbox()
    today = datetime.now().strftime("%Y-%m-%d")
    output_path = inbox / f"{today}-brief.md"

    existing = existing_brief(today)
    if existing and os.getenv("BRIEF_OVERWRITE") != "1":
        raise FileExistsError(
            f"{existing.name} already exists ({existing}); set BRIEF_OVERWRITE=1 to replace it"
        )

    markdown = format_brief(result, today, status=status, notices=notices, sources_down=sources_down,
                            sources_stale=sources_stale)
    output_path.write_text(markdown, encoding="utf-8")
    logger.info(f"Wrote brief to {output_path}")

    # A failure note from an earlier attempt today that hasn't been delivered
    # yet is moot now that the brief exists.
    stale = inbox / failure_note_name(today)
    if stale.exists():
        stale.unlink()
        logger.info(f"Removed undelivered {stale.name}: this run succeeded")
    return output_path


def write_failure_note(reason: str, run_dir: str | None = None) -> Path | None:
    """Drop YYYY-MM-DD-brief-FAILED.md into the inbox so a failed run is seen.
    Best-effort — returns None if even this can't be written."""
    try:
        inbox = _inbox()
        today = datetime.now().strftime("%Y-%m-%d")
        path = inbox / failure_note_name(today)
        lines = [
            "---",
            "type: news-brief-failure",
            f"date: {today}",
            "status: fail",
            "---",
            f"# ⚠️ News brief FAILED — {today}",
            "",
            f"The run on EVO-X2 at {datetime.now().strftime('%H:%M')} did not produce a brief.",
            "",
            f"**Reason:** {reason}",
            "",
            "Next: if this was the 07:00 run, the catch-up tries once more between 07:15 and 22:00. "
            "Details are in `~/projects/news_agg/cron.log` on EVO-X2"
            + (f" and in the run record `{run_dir}/run.json`." if run_dir else "."),
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Wrote failure note to {path}")
        return path
    except Exception as e:  # noqa: BLE001 — never mask the original failure
        logger.error(f"Could not write the failure note: {e}")
        return None


def format_brief(
    result: TriageResult,
    date_str: str,
    status: str = "ok",
    notices: list[str] | None = None,
    sources_down: list[SourceStatus] | None = None,
    sources_stale: list[SourceStatus] | None = None,
) -> str:
    """Format a TriageResult as markdown."""
    engine = getattr(result, "engine", "claude")
    lines = [
        "---",
        "type: news-brief",
        f"date: {date_str}",
        f"status: {status}",
        f"engine: {engine}",
        "---",
        f"# Morning Brief — {date_str}",
        "",
    ]
    # Anything degraded goes at the very top — Ian should know at a glance when
    # he's reading the local model's output, or when feeds have gone dead.
    for notice in notices or []:
        lines.append(f"> ⚠️ {notice}")
    if notices:
        lines.append("")
    lines += [
        f"*{result.summary}*",
        "",
        f"**{len(result.picks)} picks from {result.article_count_in} articles** · tick what was worth your time",
        "",
        "---",
        "",
    ]

    # Group picks by category for readability
    by_category: dict[str, list] = {}
    for pick in result.picks:
        by_category.setdefault(pick.category, []).append(pick)

    sorted_categories = sorted(
        by_category.keys(),
        key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99
    )

    for category in sorted_categories:
        lines.append(f"## {category.upper()}")
        lines.append("")
        for pick in sorted(by_category[category], key=lambda p: -p.interest_score):
            lines.append(f"- [ ] **[{pick.title}]({pick.url})**")
            lines.append(f"  *{pick.source}* — score {pick.interest_score}/10")
            if pick.summary:
                lines.append(f"  > {pick.summary}")
            lines.append("")

    lines.append("---")
    lines.append("")
    if sources_down:
        down = " · ".join(f"{s.name} ({s.error})" for s in sources_down)
        lines.append(f"Sources down today: {down}")
        lines.append("")
    if sources_stale:
        stale = " · ".join(f"{s.name} ({(s.newest_age_h or 0) / 24:.0f} days)" for s in sources_stale)
        lines.append(f"Feeds with no new post in far longer than usual: {stale}")
        lines.append("")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · {engine}*")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from aggregator.fetch import fetch_all
    from aggregator.triage import triage

    articles = fetch_all()
    result = triage(articles)
    path = write_brief(result)
    print(f"\nBrief written to: {path}")
    print(f"Open in Obsidian or run: open '{path}'")
