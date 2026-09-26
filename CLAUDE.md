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

Tests are offline (fake HTTP, fake Claude client) and use only `unittest`: `.venv/bin/python -m unittest discover -s tests -v`. Run them before every deploy. There is no linter or build step; `requirements.txt` is the source of truth for dependencies.

**Never run `aggregator.main` against the real inbox to try something.** For a dry run, point it elsewhere — env vars now win over `.env` (see Environment): `OBSIDIAN_VAULT_PATH=/tmp/x BRIEF_HISTORY_DIRS=$HOME/news_agg_out/delivered NEWS_AGG_RUNS_DIR=/tmp/x/runs python -m aggregator.main`. The Mac's own `.env` points at `~/news_agg_devout`, not the vault.

## Architecture

Three-stage pipeline, each stage in its own module under `aggregator/`:

1. **`fetch.py`** — Loads `sources.yaml` (RSS URL list with `category`, `weight`, optional `max_items`, `last_reviewed` per source), downloads each feed with **httpx under hard limits** (10 s connect, 30 s between bytes, 60 s total — `feedparser.parse(url)` had no timeout and once took 331 s), parses with `feedparser`, strips HTML **before** truncating summaries, filters entries to the last 24h, caps a feed at `max_items` (newest first), and returns a `FetchResult`: the articles plus a `SourceStatus` per feed (`HTTP 404`, `timed out`, `not a parseable feed`, window-truncated). A bad feed never stops the run, but it is reported, not swallowed.
2. **`triage.py`** — Loads the prompt from `prompts/triage.md`, formats articles as a numbered block, and triages via **two engines** (see "Triage engines & fallback" below): Claude primary, EVO-X2 local Ollama fallback. Both use structured output (a JSON schema the API/Ollama enforces). Picks are then sorted by score, de-duplicated by URL and capped at `MAX_PICKS = 12` in code (the model pads to ~12 whatever the day). Returns a `TriageResult` with the engine, the fallback reason, the exact prompts sent and request metadata (usage, stop_reason, request_id, attempts).
3. **`output.py`** — Formats `TriageResult` as markdown: YAML frontmatter (`type: news-brief`, `status: ok|degraded`, `engine`), a ⚠️ banner line per problem, picks as **checkboxes** (`- [ ] **[title](url)**` — Ian ticks what was worth reading) grouped by category and sorted by score, and a "Sources down today" footer. Writes `$OBSIDIAN_VAULT_PATH/$OBSIDIAN_BRIEF_FOLDER/YYYY-MM-DD-brief.md`, refusing to replace an existing or already-delivered brief unless `BRIEF_OVERWRITE=1` (the vault copy may carry ticks). On failure it writes `YYYY-MM-DD-brief-FAILED.md` instead — deliberately not `*-brief.md`, so the catch-up and dedup never mistake it for a brief.

`main.py` glues these together and decides the run's status: **ok**, **degraded** (local fallback, zero picks, a feed dead for 3 runs in a row, or ≥25% of feeds down this run — each becomes a banner line), or **fail** (no brief; the failure note goes to the inbox). It saves a **run record** to `$OBSIDIAN_VAULT_PATH/runs/<YYYY-MM-DD_HHMMSS>/` (`run.json`, `pool.json` = every fetched article in order, `system_prompt.txt`, `user_msg.txt`, `response.txt`, `picks.json` incl. dropped picks) and writes the heartbeat `<epoch> ok|degraded|fail engine=… picks=… down=…`. A run that finds today's brief already exists stops before fetching (exit 3, no heartbeat). There is no checkpointing — a triage failure means re-running fetches from scratch.

## Deployment (runs on EVO-X2, delivers to the Mac)

Moved off the Mac on 2026-06-19. The Mac was a laptop that slept on battery at 7am, suspending the process mid-run and killing the brief; EVO-X2 is an always-on server, so generation no longer depends on the laptop being awake.

- **Generation** — EVO-X2 cron (`0 7 * * *`, America/Los_Angeles) runs `scripts/run_and_deliver.sh`: `cd ~/projects/news_agg && timeout --kill-after=60 45m env PYTHONPATH=. .venv/bin/python -m aggregator.main` (which writes the ops-dashboard heartbeat `~/.ops-heartbeats/newsbrief` itself), then delivers. If the 45 min limit kills the run, the script writes the failure note and a `fail` heartbeat itself. The brief is written to a **local staging dir** on EVO-X2 (`~/news_agg_out/00-Inbox/`, set via `OBSIDIAN_VAULT_PATH`). `run_and_deliver.sh` holds a flock on `~/.local/state/news_agg/run.lock` for the whole run, so a second run (cron, catch-up, or by hand) exits 75 instead of starting — two runs could both load the fallback model, and a second run overwrites the day's brief.
- **Catch-up** — cron never re-fires a missed slot: on 2026-09-25 EVO-X2 crashed at 03:26, came back at 12:25, and there was no brief until a hand re-run at 21:56. `scripts/catchup.sh --apply` runs every 15 min (`*/15`, log `catchup.log`): between 07:15 and 22:00, if today's brief is in neither `00-Inbox/` nor `delivered/`, it runs `run_and_deliver.sh` once. That covers a missed slot and a failed 07:00 run. At most one attempt a day — the date goes into `~/.local/state/news_agg/catchup_last_attempt` before launching, so a failing catch-up isn't retried every tick (delete that file, or re-run by hand, to force another). It takes the run lock before its final brief check and hands it down (`NEWS_AGG_RUN_LOCK_HELD=1`), so it can't overlap or duplicate a run, and it defers without spending the attempt while `free -g` shows under 50 GB available (fallback model ~23 GB + 25 GB headroom). No-op ticks log nothing. Without `--apply` it's a dry run — `scripts/catchup.sh` by hand prints what it would do.
- **Delivery** — `scripts/deliver.sh` rsyncs each brief (and any `*-brief-FAILED.md`) over SSH/Tailscale to a **non-TCC** staging dir on the Mac (`~/news_agg_inbox/`), then moves the EVO-X2 copy to `~/news_agg_out/delivered/`. Idempotent + self-healing: a second cron (`*/15 * * * *`) re-runs delivery, so if the Mac was asleep at 7am the brief lands as soon as the Mac is reachable. One delivery at a time (`deliver.lock`), rsync/ssh timeouts, and the real rsync error in `deliver.log`. After delivering a brief it pulls the Mac's `~/news_agg_feedback/ticks.json` into `~/news_agg_out/feedback/`. Why staging, not direct: macOS TCC blocks SSH-spawned processes from writing `~/Documents`.
- **Vault move (Mac side)** — a launchd agent (`scripts/mac/com.iangarlington.newsbrief.mover.plist`, runs `scripts/mac/news_agg_move_brief.py`, installed as `~/bin/news_agg_move_brief.py`, every 120s) moves `*-brief.md` and `*-brief-FAILED.md` from `~/news_agg_inbox/` into `~/Documents/obsidian/myvault/00-Inbox/`. It is also the **noise**: a failure note or a `status: degraded` brief pops a macOS notification; no brief by 09:30 Pacific pops "News brief missing" once (the only signal when EVO-X2 itself is down); and it snapshots the pick checkboxes of the last 30 days of briefs into `~/news_agg_feedback/ticks.json`. The Mac's **own** launchd can write `~/Documents` (the first run triggers a one-time TCC consent for the mover; once allowed it persists). Logs to `~/Library/Logs/news_agg_mover.log`.
- **Deploying** — commit on a branch in the Mac checkout, run the tests, merge to `main`, `git push`, then on EVO-X2 `cd ~/projects/news_agg && git pull --ff-only` (never while a run holds `run.lock`). A changed mover also needs `cp scripts/mac/news_agg_move_brief.py ~/bin/` on the Mac (launchd picks it up on the next 120 s tick).

The old on-Mac launchd generator is retired (`~/Library/LaunchAgents/com.iangarlington.newsbrief.plist.disabled-*`). EVO-X2 logs: `~/projects/news_agg/cron.log` (generation, including catch-up runs), `catchup.log` (catch-up decisions) and `deliver.log` (delivery).

## Environment

`.env` (not committed; template in `.env.example`):
- `ANTHROPIC_API_KEY`
- `OBSIDIAN_VAULT_PATH` — absolute path (on EVO-X2: the local staging dir `/home/ian/news_agg_out`)
- `OBSIDIAN_BRIEF_FOLDER` — defaults to `00-Inbox`
- `OLLAMA_HOST` — fallback Ollama endpoint (EVO-X2: `http://localhost:11434`; default `http://evo-x2:11434`)
- `TRIAGE_LOCAL_MODEL` — fallback model (default `qwen3.6:35b-a3b`)
- `TRIAGE_LOCAL_FALLBACK` — set `0` to disable the local fallback

- `BRIEF_HISTORY_DIRS` — dirs scanned for the last 5 briefs by cross-day dedup (EVO-X2: `00-Inbox` + `delivered`)
- `TRIAGE_MODEL` — Claude model override; must be a key of `MODEL_PARAMS` in `triage.py` or the run fails at startup
- `NEWS_AGG_RUNS_DIR` — run-record dir (default `$OBSIDIAN_VAULT_PATH/runs`)
- `BRIEF_OVERWRITE=1` — allow replacing today's existing brief (by-hand re-runs only)

`triage.load_env()` loads `.env` **without** overriding variables already set, so a dry run's or an experiment's env vars win (with `override=True` they were silently replaced, sending test output to the real inbox). The one exception: Ian's interactive shell exports `ANTHROPIC_API_KEY=` (empty), so an empty key is filled from `.env`. Cron is unaffected (clean env).

## Triage engines & fallback

`triage()` tries **Claude** (`claude-sonnet-4-6` by default, structured output, `max_tokens` 16000) first. Request parameters come from `MODEL_PARAMS`, one entry per model, because they differ and a wrong one is a 400: Sonnet 4.6 / Sonnet 5 take `effort=low` + thinking disabled, Opus 5.5 can't disable thinking (omitted, effort low), Haiku 4.5 has no `effort`. Errors are routed: stalls, connection errors, 429/5xx/529 and mid-stream `overloaded_error` are retried (3 attempts); a 400/401/403/404 or a refusal is a `ClaudeRequestError` and a `max_tokens` cut is a `ClaudeTruncatedError` — no retry. On any total Claude failure it falls back to **EVO-X2 local Ollama** (`qwen3.6:35b-a3b`, same prompt + schema via Ollama's `format`, `think: false`, `num_ctx` sized from the prompt up to 64k, refused when the box has < 30 GB available and the model isn't loaded). The brief then carries a ⚠️ banner **with the Claude error**, so a bad request (e.g. a model swap) can't pass for a bad Anthropic morning. Both paths produce schema-valid JSON by construction (structured output), so an article title with embedded quotes can't break parsing.

Failure-mode history is in the git log — the short version: streaming (not `messages.create`) is mandatory, structured output is mandatory, and the local fallback exists because Anthropic has multi-hour bad mornings that no retry tuning survives.

## Editing sources

`sources.yaml` is meant to be edited freely — no code changes needed. Keep the `last_reviewed` date current; broken feeds get logged to `BROKEN_FEEDS.md` with a hypothesis and a next step, not silently deleted. Use `max_items` to cap a high-volume feed. A feed failing 3 runs in a row puts a "Dead feeds" banner on every brief until it's fixed or removed. Categories must be in `TRIAGE_SCHEMA`'s enum (`triage.py`); section order is `CATEGORY_ORDER` in `output.py` (others fall to the end).

## Judging a change to the prompt, model or sources

Identical re-runs share only ~9 of 12 picks (Jaccard 0.50–0.71, measured 2026-09-25), so **one run proves nothing**. Compare at least 3 runs per arm on at least 3 saved pools (`runs/*/pool.json` + `user_msg.txt` replay exactly what production saw), by story rather than by URL, against that noise floor. Ian's ticks (`~/news_agg_out/feedback/ticks.json`) are the ground truth once they accumulate.

## Long-request fragility

The triage call sends ~150–220 articles and asks for a ~4000-token JSON response. This was originally non-streaming with a 120s read timeout and was failing intermittently for weeks — the retry logic in the Anthropic SDK was masking it until 2026-05-15, when all three retries timed out. Streaming was the fix: token-by-token bytes keep the httpx read timer from tripping.

If you change the triage call, **keep it streaming**. Don't revert to `messages.create` "for simplicity" — the failure mode is delayed and silent (an 18-minute "successful" run is actually 8 internal retries).
