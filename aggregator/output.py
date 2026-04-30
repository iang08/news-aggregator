"""
output.py — Writes the triage result to a markdown file in Obsidian vault.

Format: simple navigable list. User clicks links, reads in browser,
highlights/saves via Web Clipper. No pre-synthesis here.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from aggregator.triage import TriageResult

logger = logging.getLogger(__name__)


def write_brief(result: TriageResult) -> Path:
    """Format the triage result as markdown and write to Obsidian vault."""
    load_dotenv()
    
    vault_path = os.getenv("OBSIDIAN_VAULT_PATH")
    inbox_folder = os.getenv("OBSIDIAN_BRIEF_FOLDER", "00-Inbox")
    
    if not vault_path:
        raise RuntimeError("OBSIDIAN_VAULT_PATH not set in .env")
    
    vault = Path(vault_path)
    if not vault.exists():
        raise FileNotFoundError(f"Obsidian vault not found at {vault_path}")
    
    inbox = vault / inbox_folder
    inbox.mkdir(parents=True, exist_ok=True)
    
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}-brief.md"
    output_path = inbox / filename
    
    markdown = format_brief(result, today)
    output_path.write_text(markdown, encoding="utf-8")
    
    logger.info(f"Wrote brief to {output_path}")
    return output_path


def format_brief(result: TriageResult, date_str: str) -> str:
    """Format a TriageResult as markdown."""
    lines = [
        f"# Morning Brief — {date_str}",
        "",
        f"*{result.summary}*",
        "",
        f"**{len(result.picks)} picks from {result.article_count_in} articles**",
        "",
        "---",
        "",
    ]
    
    # Group picks by category for readability
    by_category: dict[str, list] = {}
    for pick in result.picks:
        by_category.setdefault(pick.category, []).append(pick)
    
    # Order categories: ai → tech → world → japan → science → philosophy → cars
    category_order = ["ai", "tech", "world", "japan", "science", "philosophy", "cars"]
    sorted_categories = sorted(
        by_category.keys(),
        key=lambda c: category_order.index(c) if c in category_order else 99
    )
    
    for category in sorted_categories:
        lines.append(f"## {category.upper()}")
        lines.append("")
        for pick in by_category[category]:
            lines.append(f"- **[{pick.title}]({pick.url})**")
            lines.append(f"  *{pick.source}* — score {pick.interest_score}/10")
            if pick.summary:
                lines.append(f"  > {pick.summary}")
            lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    
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
