"""
main.py — Entry point for the news aggregator pipeline.

Runs: fetch RSS → Claude triage → write brief to Obsidian.

Usage:
    python -m aggregator.main
"""

from __future__ import annotations

import logging
import os
import sys

from aggregator.fetch import fetch_all
from aggregator.triage import triage
from aggregator.output import write_brief


def setup_logging() -> None:
    """Configure logging for the run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run() -> int:
    """Run the full pipeline. Returns exit code: 0 for success, non-zero for failure."""
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("=== News aggregator run starting ===")
    
    try:
        # Step 1: Fetch articles from RSS feeds
        articles = fetch_all()
        if not articles:
            logger.warning("No articles fetched. Nothing to triage.")
            return 1
        
        # Step 2: Send to Claude for triage
        result = triage(articles)
        if not result.picks:
            logger.warning("Claude returned no picks. Brief will be empty.")
        
        # Step 3: Write the brief to Obsidian
        path = write_brief(result)
        
        logger.info(f"=== Run complete. Brief at: {path} ===")
        return 0
        
    except Exception as e:
        logger.exception(f"Pipeline failed: {e}")
        return 1


def _write_heartbeat(rc: int) -> None:
    """Record an ops-dashboard heartbeat so the health dashboard can confirm this run.
    Best-effort — monitoring must never break the brief."""
    import time
    try:
        d = os.path.expanduser("~/.ops-heartbeats")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "newsbrief"), "w") as f:
            f.write("%d %s\n" % (int(time.time()), "ok" if rc == 0 else "fail"))
    except Exception:
        pass


if __name__ == "__main__":
    rc = run()
    _write_heartbeat(rc)
    sys.exit(rc)
