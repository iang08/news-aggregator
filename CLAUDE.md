# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal morning-brief pipeline for Ian Garlington. Fetches RSS feeds, asks Claude Sonnet (with a local-LLM fallback) to pick the 8–12 articles most worth Ian's attention, writes the result as a markdown file into an Obsidian vault inbox. Runs at 07:00 PDT daily **on EVO-X2** (an always-on Linux server) via cron, and delivers the brief to the Mac's Obsidian vault over SSH/Tailscale.

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
2. **`triage.py`** — Loads the prompt from `prompts/triage.md`, formats articles as a numbered block, and triages via **two engines** (see "Triage engines & fallback" below): Claude primary, EVO-X2 local Ollama fallback. Both use structured output (a JSON schema the API/Ollama enforces). Returns a `TriageResult` (with an `engine` field recording which produced it).
3. **`output.py`** — Formats `TriageResult` as markdown grouped by category, writes to `$OBSIDIAN_VAULT_PATH/$OBSIDIAN_BRIEF_FOLDER/YYYY-MM-DD-brief.md`. On EVO-X2 that's a local staging dir; a fallback brief gets a visible "⚠️ local fallback" banner.

`main.py` glues these together, configures logging, and is the run entrypoint. The pipeline is fail-fast: any exception is caught, logged with traceback, and exits non-zero. There is no checkpointing — a triage failure means re-running fetches from scratch.

## Deployment (runs on EVO-X2, delivers to the Mac)

Moved off the Mac on 2026-06-19. The Mac was a laptop that slept on battery at 7am, suspending the process mid-run and killing the brief; EVO-X2 is an always-on server, so generation no longer depends on the laptop being awake.

- **Generation** — EVO-X2 cron (`0 7 * * *`, America/Los_Angeles) runs `scripts/run_and_deliver.sh`: `cd ~/projects/news_agg && PYTHONPATH=. .venv/bin/python -m aggregator.main`, then beats the ops-dashboard heartbeat (`~/.ops-heartbeats/beat.sh news_brief`), then delivers. The brief is written to a **local staging dir** on EVO-X2 (`~/news_agg_out/00-Inbox/`, set via `OBSIDIAN_VAULT_PATH`).
- **Delivery** — `scripts/deliver.sh` rsyncs each brief over SSH/Tailscale to a **non-TCC** staging dir on the Mac (`~/news_agg_inbox/`), then moves the EVO-X2 copy to `~/news_agg_out/delivered/`. Idempotent + self-healing: a second cron (`*/15 * * * *`) re-runs delivery, so if the Mac was asleep at 7am the brief lands as soon as the Mac is reachable. Why staging, not direct: macOS TCC blocks SSH-spawned processes from writing `~/Documents`.
- **Vault move (Mac side)** — a launchd agent (`scripts/mac/com.iangarlington.newsbrief.mover.plist`, runs `scripts/mac/news_agg_move_brief.sh` every 120s) moves `*-brief.md` from `~/news_agg_inbox/` into `~/Documents/obsidian/myvault/00-Inbox/`. The Mac's **own** launchd can write `~/Documents` (the first run triggers a one-time TCC consent for the mover; once allowed it persists). Logs to `~/Library/Logs/news_agg_mover.log`.

The old on-Mac launchd generator is retired (`~/Library/LaunchAgents/com.iangarlington.newsbrief.plist.disabled-*`). EVO-X2 logs: `~/projects/news_agg/cron.log` (generation) and `deliver.log` (delivery).

## Environment

`.env` (not committed; template in `.env.example`):
- `ANTHROPIC_API_KEY`
- `OBSIDIAN_VAULT_PATH` — absolute path (on EVO-X2: the local staging dir `/home/ian/news_agg_out`)
- `OBSIDIAN_BRIEF_FOLDER` — defaults to `00-Inbox`
- `OLLAMA_HOST` — fallback Ollama endpoint (EVO-X2: `http://localhost:11434`; default `http://evo-x2:11434`)
- `TRIAGE_LOCAL_MODEL` — fallback model (default `qwen3.6:35b-a3b`)
- `TRIAGE_LOCAL_FALLBACK` — set `0` to disable the local fallback

`load_dotenv(override=True)` is used in `triage.py` deliberately: Ian's interactive shell exports `ANTHROPIC_API_KEY=` (empty), which would otherwise win over the `.env` value. Cron is unaffected (clean env).

## Triage engines & fallback

`triage()` tries **Claude** (`claude-sonnet-4-6`, `effort=low`, thinking disabled, structured output) first; on a *total* Claude failure (stalls/timeouts/parse all exhausted) it falls back to **EVO-X2 local Ollama** (`qwen3.6:35b-a3b`, same prompt + schema via Ollama's `format`). Slower (~4–5 min) and slightly lower quality, but deterministic and immune to Anthropic-side stalls. Both paths produce schema-valid JSON by construction (structured output), so an article title with embedded quotes can't break parsing.

Failure-mode history is in the git log — the short version: streaming (not `messages.create`) is mandatory, structured output is mandatory, and the local fallback exists because Anthropic has multi-hour bad mornings that no retry tuning survives.

## Editing sources

`sources.yaml` is meant to be edited freely — no code changes needed. Keep the `last_reviewed` date current; broken feeds get logged to `BROKEN_FEEDS.md` with a hypothesis and a next step, not silently deleted. Categories used by `output.py` ordering: `ai, tech, world, japan, science, philosophy, cars` (others fall to the end). If you add a category, update the `category_order` list in `output.py:69`.

## Long-request fragility

The triage call sends ~150–220 articles and asks for a ~4000-token JSON response. This was originally non-streaming with a 120s read timeout and was failing intermittently for weeks — the retry logic in the Anthropic SDK was masking it until 2026-05-15, when all three retries timed out. Streaming was the fix: token-by-token bytes keep the httpx read timer from tripping.

If you change the triage call, **keep it streaming**. Don't revert to `messages.create` "for simplicity" — the failure mode is delayed and silent (an 18-minute "successful" run is actually 8 internal retries).
