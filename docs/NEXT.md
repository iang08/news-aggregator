# Next tier — runs once ≥3 production runs are saved

Written 2026-09-26 after the audit (`docs/audit-2026-09-25.md`) and the "Now" fixes. A scheduled session picks this up; it starts fresh, so everything it needs is here.

## State on 2026-09-26

- **Code:** `origin/main` has the Now fixes, the model switch and the 51-source set. Production on EVO-X2 updates only when Ian runs the deploy (Claude's auto mode refuses production deploys — always hand Ian the command):
  `ssh ian@evo-x2 'cd ~/projects/news_agg && flock -n ~/.local/state/news_agg/run.lock git pull --ff-only && .venv/bin/python -m unittest discover -s tests'`
  The Mac mover (`~/bin/news_agg_move_brief.py`) is already the new version.
- **Engines:** primary `claude-opus-5-5`, backup `claude-sonnet-4-6`, local fallback `qwen3.6:35b-a3b` (Ollama on EVO-X2). Model check 2026-09-26: `docs/modelcheck-2026-09-26.md`.
- **Qwen3.8 Flash-Next** as the local model: untested. It needs ~105 GB, so it can only run in a GECK window (GECK V2 already runs its own Qwen3.6-35B llama-server). Ian has to approve a window; it can never be the unattended 07:00 fallback while GECK is up.
- **Ian's decisions (2026-09-26):**
  - Focus: Kyberna, Javan, JCF, Hivemaker, the autonomous-researcher design project, Deleuze and AI, fitness and health science.
  - Japan: less geopolitics, more general Japanese news from varied quality outlets, plus an occasional essay by a brilliant Japanese thinker.
  - Checkbox feedback: yes (briefs now have `- [ ]` picks; ticks land in `~/news_agg_out/feedback/<date>.json` on EVO-X2).
  - Chrome-history click check: approved by Ian, but the auto-mode classifier refuses it — Ian runs it himself (see below).
  - Phone delivery: no; laptop is fine.
  - 日経ビジネス電子版 left out: 9/9 sampled articles paywalled. Add it only if Ian subscribes.

## 0. Preconditions (stop and report if either fails)

1. EVO-X2 production is deployed: `ssh ian@evo-x2 'git -C ~/projects/news_agg log --oneline -1'` shows the 2026-09-26 commits (message starts "Opus 5.5 default…" or later). If not, tell Ian the deploy command above and stop.
2. At least 3 run records from 07:00 runs: `ssh ian@evo-x2 'ls ~/news_agg_out/runs/'` — dirs named `YYYY-MM-DD_07*` with a non-empty `user_msg.txt`. If fewer, report how many and stop.

Work in a fresh worktree off `origin/main` on the Mac (other sessions share `~/projects/news_agg`; check `git branch --show-current`, never switch its branch). Heavy work runs on EVO-X2 — pre-flight `ollama ps; free -g` as its own step first.

## 1. Prompt A/B — re-check on production pools (audit X1)

**Done 2026-09-26 on replayed pools** (`docs/promptab-2026-09-26.md`): v2 beat v1 on all 3 pools (every v2 rep above every v1 rep), 0% geopolitics, venture named in 75% of why-lines; it went live as `prompts/triage.md` (v1 kept as `prompts/triage_v1.md`) with four untested wording fixes from the verdict. Re-check now on fresh production pools that share no articles:

1. Dry run: `python -m aggregator.replay --runs ~/news_agg_out/runs/2026-09-2*_07* --prompt prompts/triage_v1.md --prompt prompts/triage.md --reps 3 --out ~/news_agg_audit/replay/v2prod` (on EVO-X2, cwd `~/projects/news_agg`). Then `--apply`.
2. Judge blind as before (items files from `pool.json`, 3 judges, 0-3 + geopolitics flag), and check the wording fixes held: no "The most useful item today is", no "bears directly on", no two picks on one story, "you" instead of "Ian".
3. Report to Ian. If the live prompt lost, say so plainly and recommend reverting.

## 2. Source trial (early read)

From `runs/*/run.json` (per-source status) and `picks.json`: per feed — items in pool, picks, pick rate, failures, stale flags, capped counts. After only 3–4 days, report; don't drop anything yet. Real verdicts after ~14 runs (keep: ≥1 pick and not mostly unticked; tighten: pool share >3× pick share; drop: 0 picks from ≥10 items or a persistent dead/stale flag; feeds under 0.2/day get 8 weeks).

## 2b. People feeds and paywall filters (added 2026-09-26)

36 people feeds (`docs/people-2026-09-26.md`) and the paywall filters went in on 2026-09-26. In the early source read, report per people feed: items, picks, paid_skipped, and whether it was stale; and per filter: how many items each rule dropped (run.json `sources[].paid_skipped`). **PubMed:** both saved-search feeds returned an empty channel on the evening of 09-26 after working that afternoon (NCBI throttling EVO-X2, or the saved searches expiring). If they are still empty, replace them with NCBI E-utilities (esearch + esummary for the same queries, free-full-text filter) rather than the RSS.

## 3. Dynamic fetch window (audit X3) — DONE (e44112f)

`main.fetch_window_hours`: hours since the newest earlier `*-brief.md` was written (file mtime, not `run.json`) + 0.5, clamped to 28–48 h; per-feed `window_hours` only reaches further back. Recorded as `hours_back` in `run.json`.

## 4. Feedback

Summarize ticks so far (`~/news_agg_out/feedback/*.json`): tick rate by source, category and score. If Ian hasn't ticked anything yet, say so — don't infer.

## 5. Deliverable

A short report to Ian (numbers, recommendation, what needs his OK) and a branch with any code changes, tested. Don't deploy to production; hand Ian the commands.

## For Ian: the click check (run it yourself)

It matches brief picks against a copy of Chrome's history and prints only aggregate counts per source/category/score. Close Chrome first, then:

```
cp ~/Library/Application\ Support/Google/Chrome/Default/History /tmp/History.copy && python3 ~/projects/news_agg/docs/clicks.py /tmp/History.copy; rm /tmp/History.copy
```
