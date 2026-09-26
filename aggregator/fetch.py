"""
fetch.py — Pulls RSS feeds and returns articles from the last N hours.

Loads source config from sources.yaml, downloads each feed with a hard time
limit, parses it with feedparser, filters by recency, and reports per-source
status (so a dead feed is visible instead of silently contributing nothing).
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import httpx
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

# Per-feed time limits. feedparser.parse(url) has no timeout at all: 3:AM
# Magazine once took 331 s and HN 133 s, and since the run holds a lock, one
# hung feed would block the day's brief and every catch-up (2026-09-25 audit).
# So we download with httpx and hand feedparser the bytes.
FEED_CONNECT_TIMEOUT_S = 10.0
FEED_READ_TIMEOUT_S = 30.0   # max silence between bytes
FEED_DEADLINE_S = 60.0       # whole download, however slowly it trickles in
FEED_MAX_BYTES = 10_000_000

SUMMARY_MAX_CHARS = 1000

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_WS_RE = re.compile(r"\s+")


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


@dataclass
class SourceStatus:
    """What happened to one feed on one run — feeds the brief footer, the
    heartbeat and the saved run record."""
    name: str
    category: str
    ok: bool
    articles: int = 0
    error: str = ""                # e.g. "HTTP 404", "no response within 60s"
    http_status: int | None = None
    seconds: float = 0.0
    capped: int = 0                # in-window entries dropped by max_items
    window_truncated: bool = False  # feed's own item cap ends inside the window


@dataclass
class FetchResult:
    articles: list[Article]
    sources: list[SourceStatus] = field(default_factory=list)

    @property
    def failed(self) -> list[SourceStatus]:
        return [s for s in self.sources if not s.ok]


class FeedError(Exception):
    """A feed could not be fetched or parsed; the message is shown to Ian."""


def clean_text(s: str | None) -> str:
    """HTML -> plain text: drop script/style, strip tags, unescape entities,
    collapse whitespace. Done BEFORE any truncation — cutting raw HTML first left
    most Hacker News and r/LocalLLaMA items as headline-only (audit F7)."""
    if not s:
        return ""
    s = _SCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", html.unescape(s)).strip()


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
        src.setdefault("max_items", 0)  # 0 = no per-run cap

    return sources


def _download(url: str) -> tuple[int, dict[str, str], str, bytes]:
    """GET a feed within FEED_DEADLINE_S. Returns (status, headers, final_url, body)."""
    t0 = time.monotonic()
    timeout = httpx.Timeout(FEED_READ_TIMEOUT_S, connect=FEED_CONNECT_TIMEOUT_S)
    headers = {
        # feedparser's own UA — the one every current feed has been accepting.
        "User-Agent": feedparser.USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, headers=headers) as r:
            chunks: list[bytes] = []
            size = 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if time.monotonic() - t0 > FEED_DEADLINE_S:
                    raise FeedError(f"no complete response within {FEED_DEADLINE_S:.0f}s")
                if size > FEED_MAX_BYTES:
                    raise FeedError(f"feed larger than {FEED_MAX_BYTES // 1_000_000} MB")
            return r.status_code, dict(r.headers), str(r.url), b"".join(chunks)
    except httpx.TimeoutException as e:
        raise FeedError(f"timed out ({type(e).__name__})") from e
    except httpx.HTTPError as e:
        raise FeedError(f"{type(e).__name__}: {e}"[:160]) from e


def fetch_source(source: dict[str, Any], cutoff: datetime) -> tuple[list[Article], SourceStatus]:
    """Fetch a single feed and return (articles published after cutoff, status)."""
    name = source["name"]
    status = SourceStatus(name=name, category=source["category"], ok=False)
    logger.info(f"Fetching: {name}")
    t0 = time.monotonic()

    try:
        http_status, headers, final_url, body = _download(source["url"])
        status.http_status = http_status
        if http_status >= 400:
            raise FeedError(f"HTTP {http_status}")
        feed = feedparser.parse(body, response_headers={**headers, "content-location": final_url})
        # No entries AND (a parse error, or no RSS/Atom version at all): an
        # HTML page served with 200 — a soft 404, a bot check — parses cleanly
        # as "a feed with nothing in it" and would otherwise look healthy.
        if not feed.entries and (feed.bozo or not feed.get("version")):
            ctype = headers.get("content-type", "?").split(";")[0]
            raise FeedError(f"not a parseable feed (HTTP {http_status}, {ctype})")
    except FeedError as e:
        status.error = str(e)
        status.seconds = round(time.monotonic() - t0, 1)
        logger.warning(f"  ✗ {name}: {status.error}")
        return [], status

    articles: list[Article] = []
    oldest_dated: datetime | None = None
    for entry in feed.entries:
        # Get published time — feedparser exposes it as a struct_time
        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published_parsed:
            continue  # Skip entries with no date — can't filter by recency

        published = datetime(*published_parsed[:6], tzinfo=timezone.utc)
        if oldest_dated is None or published < oldest_dated:
            oldest_dated = published
        if published < cutoff:
            continue  # Too old

        summary = entry.get("summary") or ""
        if not summary and entry.get("content"):
            summary = entry["content"][0].get("value", "")
        articles.append(Article(
            title=clean_text(entry.get("title")) or "(no title)",
            link=entry.get("link", ""),
            summary=clean_text(summary)[:SUMMARY_MAX_CHARS],
            published=published,
            source_name=name,
            source_category=source["category"],
            source_weight=source["weight"],
        ))

    # A feed that only ever returns its newest N items can end INSIDE our
    # window (Al Jazeera: 25 items ≈ 7.5 h), silently dropping the rest.
    if articles and oldest_dated is not None and oldest_dated >= cutoff and len(feed.entries) >= 10:
        status.window_truncated = True
        hours = (datetime.now(timezone.utc) - oldest_dated).total_seconds() / 3600
        logger.warning(f"  ! {name}: feed returns only {len(feed.entries)} items, covering {hours:.1f} h of the window")

    cap = int(source.get("max_items") or 0)
    if cap and len(articles) > cap:
        articles.sort(key=lambda a: a.published, reverse=True)
        status.capped = len(articles) - cap
        articles = articles[:cap]

    status.ok = True
    status.articles = len(articles)
    status.seconds = round(time.monotonic() - t0, 1)
    capped = f" (capped, {status.capped} more dropped)" if status.capped else ""
    logger.info(f"  ✓ {name}: {len(articles)} articles in window{capped}")
    return articles, status


def fetch_all_with_status(hours_back: int = 24) -> FetchResult:
    """Fetch all sources; return the combined articles plus per-source status.

    Retries the entire fetch if ALL sources returned zero articles, which is
    the signature of network-not-ready (e.g., laptop just woke from sleep
    when launchd fired the job). See MAX_FETCH_ATTEMPTS / FETCH_RETRY_BACKOFF_S.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    sources = load_sources()

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        all_articles: list[Article] = []
        statuses: list[SourceStatus] = []
        for source in sources:
            articles, status = fetch_source(source, cutoff)
            all_articles.extend(articles)
            statuses.append(status)

        failed = [s.name for s in statuses if not s.ok]
        logger.info(
            f"\nTotal: {len(all_articles)} articles from {len(sources)} sources"
            + (f"; {len(failed)} failed: {', '.join(failed)}" if failed else "")
        )

        if all_articles:
            return FetchResult(all_articles, statuses)

        if attempt == MAX_FETCH_ATTEMPTS:
            logger.error(
                f"All {len(sources)} sources returned 0 articles on attempt "
                f"{attempt}/{MAX_FETCH_ATTEMPTS}; giving up. Likely network "
                f"problem or all feeds genuinely empty."
            )
            return FetchResult(all_articles, statuses)

        logger.warning(
            f"All {len(sources)} sources returned 0 articles on attempt "
            f"{attempt}/{MAX_FETCH_ATTEMPTS}; network may not be ready yet. "
            f"Waiting {FETCH_RETRY_BACKOFF_S}s and retrying full fetch."
        )
        time.sleep(FETCH_RETRY_BACKOFF_S)

    # Unreachable — the loop always either returns or breaks. Kept for type checker.
    return FetchResult([], [])


def fetch_all(hours_back: int = 24) -> list[Article]:
    """Articles only — for the module __main__ blocks and ad-hoc experiments."""
    return fetch_all_with_status(hours_back).articles


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    articles = fetch_all()
    print(f"\n--- Top 5 article titles ---")
    for art in articles[:5]:
        print(f"[{art.source_category}] {art.source_name}: {art.title}")
