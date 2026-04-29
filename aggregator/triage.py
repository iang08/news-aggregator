"""
triage.py — Sends fetched articles to Claude, returns top picks as structured data.

Loads the prompt from prompts/triage.md, formats articles as a list,
calls the Claude API, parses the JSON response, returns a TriageResult.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from aggregator.fetch import Article

logger = logging.getLogger(__name__)

# Claude model to use. Sonnet is the right default for this kind of
# reading + structured output task. We'll revisit if costs add up or
# if quality on slow news days disappoints.
MODEL = "claude-sonnet-4-5-20250929"

# Maximum tokens Claude can return. ~12 picks × ~200 tokens each + headroom = 4000.
MAX_TOKENS = 4000


@dataclass
class TriagePick:
    """A single article Claude selected as worth Ian's attention."""
    title: str
    source: str
    category: str
    url: str
    summary: str
    interest_score: int
    tags: list[str] = field(default_factory=list)


@dataclass
class TriageResult:
    """The full output of a triage run."""
    summary: str  # One-sentence theme of the day
    picks: list[TriagePick]
    article_count_in: int  # How many articles were considered
    raw_response: str  # Claude's raw output, for debugging


def load_prompt(path: Path = Path("prompts/triage.md")) -> str:
    """Read the system prompt from disk."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found at {path}")
    return path.read_text(encoding="utf-8")


def format_articles_for_claude(articles: list[Article]) -> str:
    """Format articles as a numbered list for Claude to read."""
    lines = []
    for i, art in enumerate(articles, start=1):
        # Trim summary to keep input token count reasonable
        summary = art.summary[:300].replace("\n", " ").strip()
        lines.append(
            f"[{i}] {art.title}\n"
            f"    Source: {art.source_name} (category: {art.source_category}, weight: {art.source_weight})\n"
            f"    URL: {art.link}\n"
            f"    Summary: {summary}\n"
        )
    return "\n".join(lines)


def triage(articles: list[Article]) -> TriageResult:
    """Send articles to Claude and return the triaged result."""
    if not articles:
        raise ValueError("No articles to triage — fetch returned empty list")

    load_dotenv()  # reads .env into environment
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Check your .env file.")

    client = Anthropic(api_key=api_key)
    system_prompt = load_prompt()
    article_block = format_articles_for_claude(articles)

    logger.info(f"Sending {len(articles)} articles to Claude for triage...")

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Here are {len(articles)} articles from the last 24 hours. "
                    f"Triage them per the instructions in your system prompt.\n\n"
                    f"{article_block}"
                ),
            }
        ],
    )

    # Claude returns a list of content blocks; we want the text from the first one
    raw = message.content[0].text
    logger.info(f"Claude responded with {len(raw)} characters")

    # Parse the JSON. Be tolerant of Claude wrapping it in markdown code fences.
    json_text = raw.strip()
    if json_text.startswith("```"):
        # Strip ```json or ``` opening fence
        json_text = json_text.split("\n", 1)[1] if "\n" in json_text else json_text
        # Strip closing ``` fence
        if json_text.endswith("```"):
            json_text = json_text.rsplit("```", 1)[0]
        json_text = json_text.strip()

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from Claude. Raw response:\n{raw}")
        raise RuntimeError(f"Claude returned non-JSON output: {e}") from e

    picks = [
        TriagePick(
            title=p["title"],
            source=p["source"],
            category=p["category"],
            url=p["url"],
            summary=p["summary"],
            interest_score=int(p["interest_score"]),
            tags=p.get("tags", []),
        )
        for p in parsed.get("picks", [])
    ]

    return TriageResult(
        summary=parsed.get("summary", "(no summary)"),
        picks=picks,
        article_count_in=len(articles),
        raw_response=raw,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    from aggregator.fetch import fetch_all
    
    articles = fetch_all()
    if not articles:
        print("No articles fetched. Exiting.")
        exit(1)
    
    result = triage(articles)
    
    print(f"\n=== Today's signal ===")
    print(result.summary)
    print(f"\n=== {len(result.picks)} picks from {result.article_count_in} articles ===\n")
    
    for i, pick in enumerate(result.picks, start=1):
        print(f"{i}. [{pick.category}] {pick.source} (score: {pick.interest_score}/10)")
        print(f"   {pick.title}")
        print(f"   {pick.summary}")
        print(f"   Tags: {', '.join(pick.tags)}")
        print(f"   {pick.url}")
        print()
