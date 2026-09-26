"""
main.py — Entry point for the news aggregator pipeline.

Runs: fetch RSS → Claude triage → write brief to Obsidian, then saves a run
record and the ops-dashboard heartbeat.

Every run ends in one of three states, and each is visible where Ian looks:
  ok        the brief, nothing else
  degraded  the brief with a ⚠️ banner at the top (local fallback, no picks,
            dead feeds, many feeds down); the Mac mover also pops a notification
  fail      no brief; YYYY-MM-DD-brief-FAILED.md lands in the inbox instead,
            and the Mac mover pops a notification

Usage:
    python -m aggregator.main
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from aggregator.fetch import FetchResult, SourceStatus, fetch_all_with_status
from aggregator.triage import TriageResult, load_env, triage
from aggregator.output import existing_brief, write_brief, write_failure_note

# A feed that failed this many runs in a row is "dead" and flagged in the
# brief's banner until it is fixed or removed. Willamette Week and OPB returned
# 404 for 144 days in a log line nobody read.
DEAD_AFTER_RUNS = 3
# This share of feeds failing in one run points at the network, not the feeds.
MANY_DOWN_SHARE = 0.25


def setup_logging() -> None:
    """Configure logging for the run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def runs_dir() -> Path | None:
    """Where run records go: NEWS_AGG_RUNS_DIR, else <OBSIDIAN_VAULT_PATH>/runs
    (on EVO-X2: ~/news_agg_out/runs — never delivered to the Mac)."""
    load_env()
    explicit = os.getenv("NEWS_AGG_RUNS_DIR")
    if explicit:
        return Path(explicit)
    vault = os.getenv("OBSIDIAN_VAULT_PATH")
    return Path(vault) / "runs" if vault else None


def dead_sources(current: list[SourceStatus], runs: Path | None) -> list[SourceStatus]:
    """Sources that failed now AND in each of the previous DEAD_AFTER_RUNS-1 runs."""
    failing = {s.name: s for s in current if not s.ok}
    if not failing or runs is None or not runs.is_dir():
        return []
    previous = sorted((p for p in runs.iterdir() if (p / "run.json").is_file()), reverse=True)
    history = []
    for p in previous[: DEAD_AFTER_RUNS - 1]:
        try:
            history.append(json.loads((p / "run.json").read_text()))
        except (OSError, ValueError):
            return []  # unreadable history: don't guess
    if len(history) < DEAD_AFTER_RUNS - 1:
        return []
    for rec in history:
        failed_then = {s["name"] for s in rec.get("sources", []) if not s.get("ok")}
        failing = {n: s for n, s in failing.items() if n in failed_then}
    return list(failing.values())


def assess(fetch: FetchResult, result: TriageResult, dead: list[SourceStatus]) -> tuple[str, list[str]]:
    """(status, notices) for a run that produced a brief."""
    notices: list[str] = []
    if result.engine.startswith("local"):
        notices.append(
            f"**Local fallback** ({result.engine.split(':', 1)[1]}) wrote this brief — "
            f"Claude failed: {result.fallback_reason or 'unknown error'}"
        )
    if not result.picks:
        notices.append("The model returned **no picks** today.")
    if dead:
        names = ", ".join(f"{s.name} ({s.error})" for s in dead)
        notices.append(
            f"**Dead feeds** — failed {DEAD_AFTER_RUNS} runs in a row: {names}. "
            f"Fix or remove them in sources.yaml."
        )
    failed = fetch.failed
    if fetch.sources and len(failed) / len(fetch.sources) >= MANY_DOWN_SHARE:
        notices.append(f"**{len(failed)} of {len(fetch.sources)} feeds failed** this run — network trouble?")
    return ("degraded" if notices else "ok"), notices


def save_run_record(
    runs: Path | None,
    started: datetime,
    status: str,
    error: str,
    fetch: FetchResult | None,
    result: TriageResult | None,
    brief_path: Path | None,
    notices: list[str],
) -> Path | None:
    """Save what's needed to replay or audit this run. Best-effort."""
    if runs is None:
        return None
    try:
        d = runs / started.strftime("%Y-%m-%d_%H%M%S")
        d.mkdir(parents=True, exist_ok=True)
        record = {
            "started": started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "error": error,
            "notices": notices,
            "brief_path": str(brief_path) if brief_path else None,
            "fetched": len(fetch.articles) if fetch else 0,
            "sources": [asdict(s) for s in fetch.sources] if fetch else [],
        }
        if result is not None:
            record.update(
                engine=result.engine,
                fallback_reason=result.fallback_reason,
                article_count_in=result.article_count_in,
                picks=len(result.picks),
                dropped=len(result.dropped),
                meta=result.meta,
            )
            (d / "picks.json").write_text(json.dumps(
                {"summary": result.summary,
                 "picks": [asdict(p) for p in result.picks],
                 "dropped": [asdict(p) for p in result.dropped]},
                ensure_ascii=False, indent=1))
            (d / "response.txt").write_text(result.raw_response or "")
            (d / "system_prompt.txt").write_text(result.system_prompt or "")
            (d / "user_msg.txt").write_text(result.user_msg or "")
        if fetch is not None:
            pool = [{**asdict(a), "published": a.published.isoformat()} for a in fetch.articles]
            (d / "pool.json").write_text(json.dumps(pool, ensure_ascii=False, indent=1))
        (d / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str))
        return d
    except Exception as e:  # noqa: BLE001 — records must never break the brief
        logging.getLogger(__name__).error(f"Could not save the run record: {e}")
        return None


def run() -> tuple[int, str, str]:
    """Run the full pipeline. Returns (exit code, status, heartbeat detail)."""
    setup_logging()
    logger = logging.getLogger(__name__)
    started = datetime.now()
    runs = runs_dir()

    logger.info("=== News aggregator run starting ===")

    # Check before spending a fetch + an API call: a second run on the same day
    # must not replace a delivered brief (it may carry Ian's ticks).
    existing = existing_brief(started.strftime("%Y-%m-%d"))
    if existing and os.getenv("BRIEF_OVERWRITE") != "1":
        logger.error(f"Today's brief already exists ({existing}); not running. Set BRIEF_OVERWRITE=1 to replace it.")
        return 3, "skipped", ""

    fetch: FetchResult | None = None
    result: TriageResult | None = None
    brief_path: Path | None = None
    status, error, notices = "fail", "", []
    try:
        # Step 1: Fetch articles from RSS feeds
        fetch = fetch_all_with_status()
        if not fetch.articles:
            raise RuntimeError(
                f"all {len(fetch.sources)} feeds returned 0 articles "
                f"({len(fetch.failed)} failed outright) — network down?"
            )

        # Step 2: Send to Claude for triage
        result = triage(fetch.articles)

        # Step 3: Write the brief to Obsidian
        dead = dead_sources(fetch.sources, runs)
        status, notices = assess(fetch, result, dead)
        brief_path = write_brief(result, status=status, notices=notices, sources_down=fetch.failed)
        for n in notices:
            logger.warning(f"DEGRADED: {n}")
        logger.info(f"=== Run complete ({status}). Brief at: {brief_path} ===")
    except Exception as e:
        status = "fail"
        error = f"{type(e).__name__}: {e}"[:500]
        logger.exception(f"Pipeline failed: {e}")

    run_path = save_run_record(runs, started, status, error, fetch, result, brief_path, notices)
    if status == "fail":
        write_failure_note(error, str(run_path) if run_path else None)

    detail = " ".join(
        f"{k}={v}" for k, v in (
            ("engine", result.engine if result else "-"),
            ("picks", len(result.picks) if result else 0),
            ("down", len(fetch.failed) if fetch else "-"),
            ("notices", len(notices)),
        )
    )
    return (1 if status == "fail" else 0), status, detail


def _write_heartbeat(status: str, detail: str = "") -> None:
    """Record an ops-dashboard heartbeat: "<epoch> ok|degraded|fail <detail>".
    The dashboard reads the first two fields. Best-effort — monitoring must
    never break the brief."""
    try:
        d = os.path.expanduser("~/.ops-heartbeats")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "newsbrief"), "w") as f:
            f.write(f"{int(time.time())} {status} {detail}".rstrip() + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    rc, status, detail = run()
    if status != "skipped":  # a refused re-run says nothing about today's brief
        _write_heartbeat(status, detail)
    sys.exit(rc)
