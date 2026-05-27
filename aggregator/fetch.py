"""
fetch.py — Pulls RSS feeds and returns articles from the last N hours.

Loads source config from sources.yaml, fetches each feed with feedparser,
filters by recency, returns a flat list of articles.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import yaml

logger = logging.getLogger(__name__)

# launchd fires the job at scheduled time, but if the laptop was asleep
# the job is queued and fires on wake. Right after wake, networking may
# not be fully up — DNS, captive-portal redirects, etc. all 19 feeds
# return malformed/empty in that window. Retry the whole fetch on the
# "ALL sources returned zero" signal (which is much stronger than the
# usual "a few feeds are flaky" mode and so safe to gate the retry on).
MAX_FETCH_ATTEMPTS = 3
FETCH_RETRY_BACKOFF_S = 60


@dataclass
class Article:
    """A single article from a source feed."""
    title: str
    link: str
    summary: str
    published: datetime
    source_name: str
    source_category: str
    source_weight: float


def load_sources(path: Path = Path("sources.yaml")) -> list[dict[str, Any]]:
    """Load and validate source configs from sources.yaml."""
    if not path.exists():
        raise FileNotFoundError(f"Source config not found at {path}")
    
    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    sources = config.get("sources", [])
    if not sources:
        raise ValueError(f"No sources defined in {path}")
    
    # Basic validation — every source must have name, url, category
    for i, src in enumerate(sources):
        for required in ("name", "url", "category"):
            if required not in src:
                raise ValueError(f"Source #{i} missing required field: {required}")
        # Set defaults for optional fields
        src.setdefault("weight", 1.0)
    
    return sources


def fetch_source(source: dict[str, Any], cutoff: datetime) -> list[Article]:
    """Fetch a single feed and return articles published after cutoff."""
    name = source["name"]
    url = source["url"]
    
    logger.info(f"Fetching: {name}")
    
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        logger.warning(f"  ✗ {name}: fetch failed — {e}")
        return []
    
    if feed.bozo and not feed.entries:
        logger.warning(f"  ✗ {name}: malformed feed, no entries parsed")
        return []
    
    articles: list[Article] = []
    for entry in feed.entries:
        # Get published time — feedparser exposes it as a struct_time
        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published_parsed:
            continue  # Skip entries with no date — can't filter by recency
        
        published = datetime(*published_parsed[:6], tzinfo=timezone.utc)
        if published < cutoff:
            continue  # Too old
        
        articles.append(Article(
            title=entry.get("title", "(no title)"),
            link=entry.get("link", ""),
            summary=entry.get("summary", "")[:1000],  # cap to 1000 chars
            published=published,
            source_name=name,
            source_category=source["category"],
            source_weight=source["weight"],
        ))
    
    logger.info(f"  ✓ {name}: {len(articles)} articles in window")
    return articles


def fetch_all(hours_back: int = 24) -> list[Article]:
    """Fetch all sources and return combined article list from last N hours.

    Retries the entire fetch if ALL sources returned zero articles, which is
    the signature of network-not-ready (e.g., laptop just woke from sleep
    when launchd fired the job). See MAX_FETCH_ATTEMPTS / FETCH_RETRY_BACKOFF_S.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    sources = load_sources()

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        all_articles: list[Article] = []
        for source in sources:
            all_articles.extend(fetch_source(source, cutoff))

        logger.info(f"\nTotal: {len(all_articles)} articles from {len(sources)} sources")

        if all_articles:
            return all_articles

        if attempt == MAX_FETCH_ATTEMPTS:
            logger.error(
                f"All {len(sources)} sources returned 0 articles on attempt "
                f"{attempt}/{MAX_FETCH_ATTEMPTS}; giving up. Likely network "
                f"problem or all feeds genuinely empty."
            )
            return all_articles

        logger.warning(
            f"All {len(sources)} sources returned 0 articles on attempt "
            f"{attempt}/{MAX_FETCH_ATTEMPTS}; network may not be ready yet. "
            f"Waiting {FETCH_RETRY_BACKOFF_S}s and retrying full fetch."
        )
        time.sleep(FETCH_RETRY_BACKOFF_S)

    # Unreachable — the loop always either returns or breaks. Kept for type checker.
    return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    articles = fetch_all()
    print(f"\n--- Top 5 article titles ---")
    for art in articles[:5]:
        print(f"[{art.source_category}] {art.source_name}: {art.title}")
