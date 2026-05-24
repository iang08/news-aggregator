# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal morning-brief pipeline for Ian Garlington. Fetches RSS feeds, asks Claude Sonnet to pick the 8–12 articles most worth Ian's attention, writes the result as a markdown file into an Obsidian vault inbox. Scheduled by launchd to run at 07:00 daily on macOS.

The triage prompt at `prompts/triage.md` is highly personalized (Javan Imports / Hivemaker / Kyberna context, Japan signal, Deleuze, PDX). Treat it as the editorial heart of the project — most "quality" work is prompt tuning, not code.

## Running it

```bash
source .venv/bin/activate          # always activate the venv first
python -m aggregator.main          # full pipeline: fetch → triage → write brief
python -m aggregator.fetch         # fetch only (prints 5 sample titles)
python -m aggregator.triage        # fetch + triage, prints picks to stdout
python -m aggregator.output        # full pipeline via module main block
```

There is no test runner, linter, or build step configured. `tests/test_fetch.py` and `pyproject.toml` are empty stubs. `requirements.txt` is the source of truth for dependencies.

## Architecture

Three-stage pipeline, each stage in its own module under `aggregator/`:

1. **`fetch.py`** — Loads `sources.yaml` (RSS URL list with `category`, `weight`, `last_reviewed` per source), calls `feedparser` per feed, filters entries to the last 24h, returns `list[Article]`. Bad feeds are logged and skipped, not fatal.
2. **`triage.py`** — Loads the prompt from `prompts/triage.md`, formats articles as a numbered block, **streams** a single Claude request (`messages.stream` + `get_final_message()` — see "Long-request fragility" below), parses the JSON response into `TriageResult`.
3. **`output.py`** — Formats `TriageResult` as markdown grouped by category, writes to `$OBSIDIAN_VAULT_PATH/$OBSIDIAN_BRIEF_FOLDER/YYYY-MM-DD-brief.md`.

`main.py` glues these together, configures logging, and is the launchd entrypoint. The pipeline is fail-fast: any exception is caught, logged with traceback, and exits non-zero. There is no checkpointing — a triage failure means re-running fetches from scratch.

## Scheduling (launchd)

`scripts/com.iangarlington.newsbrief.plist` is the launchd job. It hardcodes:
- `/Users/zen/projects/news_agg/.venv/bin/python` as the interpreter
- `/Users/zen/projects/news_agg` as `WorkingDirectory` (required — `load_dotenv()` and `load_sources()` use relative paths)
- 07:00 daily via `StartCalendarInterval`

stdout → `logs/newsbrief.log`, stderr (including Python logging) → `logs/newsbrief.error.log`. The error log is the operational source of truth — it's where you go when a morning fires badly.

## Environment

`.env` (not committed; template in `.env.example`):
- `ANTHROPIC_API_KEY`
- `OBSIDIAN_VAULT_PATH` — absolute path
- `OBSIDIAN_BRIEF_FOLDER` — defaults to `00-Inbox`

`load_dotenv(override=True)` is used in `triage.py` deliberately: Ian's interactive shell exports `ANTHROPIC_API_KEY=` (empty), which would otherwise win over the `.env` value. Cron/launchd is unaffected (clean env).

## Editing sources

`sources.yaml` is meant to be edited freely — no code changes needed. Keep the `last_reviewed` date current; broken feeds get logged to `BROKEN_FEEDS.md` with a hypothesis and a next step, not silently deleted. Categories used by `output.py` ordering: `ai, tech, world, japan, science, philosophy, cars` (others fall to the end). If you add a category, update the `category_order` list in `output.py:69`.

## Long-request fragility

The triage call sends ~150–220 articles and asks for a ~4000-token JSON response. This was originally non-streaming with a 120s read timeout and was failing intermittently for weeks — the retry logic in the Anthropic SDK was masking it until 2026-05-15, when all three retries timed out. Streaming was the fix: token-by-token bytes keep the httpx read timer from tripping.

If you change the triage call, **keep it streaming**. Don't revert to `messages.create` "for simplicity" — the failure mode is delayed and silent (an 18-minute "successful" run is actually 8 internal retries).
