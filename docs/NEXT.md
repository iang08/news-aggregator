# Next tier — runs once ≥3 production runs are saved

## Status 2026-09-30 (scheduled session; branch `next-tier`)

- **Preconditions passed.** Production is at 842efe1, but it was pulled on 09-27 at 11:15, so the 09-27 07:00 run used the old code: v1 prompt and no people feeds. That leaves 3 runs on the current code (09-28 to 09-30).
- **§1 done:** `docs/promptab-2026-09-30.md`. v2 beats v1 on precision in 4/4 pools (mean 2.16 vs 1.81) and all the wording fixes held. But it finds fewer good items (5.2 vs 6.6 judged ≥2 per run) and systematically skips model releases and hands-on r/LocalLLaMA reports. Keep v2. The candidate fix is `prompts/triage_v2_1.md` (not live).
- **§2/2b early read done** (below). Nothing was dropped. HN was dead on all 4 runs, so the `next-tier` branch adds `fallback_url` (HN falls back to news.ycombinator.com/rss).
- **§4:** 0 ticks in 4 briefs. The harvester works (the mover log says `0/N picks ticked` daily, and the vault has no `[x]`).

### Next (in order)
1. **Ian:** merge `next-tier` to main, then deploy with the command below. The branch holds the HN fallback, the v2.1 candidate prompt and these docs.
2. **After the deploy, with Ian's OK (~$1.30):** A/B v2 vs v2.1 on the same 4 pools (the command is in `docs/promptab-2026-09-30.md`; v2 reps are cached). Promote v2.1 only if it recovers the release and LocalLLaMA items without lowering the mean score.
3. **~2026-10-12 (≥14 current-code runs):** real source verdicts (§2 rules), plus the tick summary if Ian has ticked. Check that HN shows up as ok or "Served by a fallback feed" and not as a Dead feeds notice.
4. **Ongoing:** tick rate stays 0 → ask Ian whether the checkbox habit is realistic; otherwise the per-source keep/drop rules have no feedback input and fall back to picks alone.

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

## 1. Prompt A/B — re-check on production pools (audit X1) — DONE 2026-09-30 (`docs/promptab-2026-09-30.md`)

**Done 2026-09-26 on replayed pools** (`docs/promptab-2026-09-26.md`): v2 beat v1 on all 3 pools (every v2 rep above every v1 rep), 0% geopolitics, venture named in 75% of why-lines; it went live as `prompts/triage.md` (v1 kept as `prompts/triage_v1.md`) with four untested wording fixes from the verdict. Re-check now on fresh production pools that share no articles:

1. Dry run: `python -m aggregator.replay --runs ~/news_agg_out/runs/2026-09-2*_07* --prompt prompts/triage_v1.md --prompt prompts/triage.md --reps 3 --out ~/news_agg_audit/replay/v2prod` (on EVO-X2, cwd `~/projects/news_agg`). Then `--apply`.
2. Judge blind as before (items files from `pool.json`, 3 judges, 0-3 + geopolitics flag), and check the wording fixes held: no "The most useful item today is", no "bears directly on", no two picks on one story, "you" instead of "Ian".
3. Report to Ian. If the live prompt lost, say so plainly and recommend reverting.

## 2. Source trial (early read) — early read DONE 2026-09-30

**Read, 4 runs (09-27 to 09-30; people feeds 3 runs):** 332 items in the pools, 35 picks, 87 feeds.
- **Picks:** r/LocalLLaMA 8/36, arXiv research agents 5/16, nippon.com 5/9, Ars 3/31, NHK 経済 3/19, Willison 2/11, and 1 each from NHK 科学, はてブ学び, Marginal Revolution, GNews 中古車輸出, Japan Times (09-27 only), HF, Import AI, Physiologically Speaking and Ben Recht.
- **Large pool share, 0 picks (on watch for "tighten"):** OPB 33, Willamette Week 26, BBC World 24 (+67 capped), Nature 9 (+25 capped, 10 paywalled), Stateline 6, New Humanitarian 5, Aeon 5. By category, local has 59 items and 0 picks, world 36 and 0, science 16 and 0.
- **Dead:** Hacker News failed 4/4 at 07:00 (ReadTimeout, ConnectTimeout ×2, HTTP 502). It answers fine in the afternoon. The fix, `fallback_url`, is on `next-tier`.
- **Stale:** Sean Morris ARB/tariffs (3/3 runs; newest post about 62 days old).
- **Failures:** arXiv research agents failed 2/4.
- **PubMed:** both feeds are OK (entries present, 4 and 3 items). No E-utilities replacement needed.

From `runs/*/run.json` (per-source status) and `picks.json`: per feed — items in pool, picks, pick rate, failures, stale flags, capped counts. After only 3–4 days, report; don't drop anything yet. Real verdicts after ~14 runs (keep: ≥1 pick and not mostly unticked; tighten: pool share >3× pick share; drop: 0 picks from ≥10 items or a persistent dead/stale flag; feeds under 0.2/day get 8 weeks).

## 2b. People feeds and paywall filters (added 2026-09-26) — early read DONE 2026-09-30

**Read:**
- **People feeds:** 36 feeds, 3 runs. 6 produced any items: hamachan 3, Gelman 3, AI as Normal Technology 2, and Ben Recht, Chips and Cheese and Sentinel 1 each. Only Ben Recht was picked (1). The other 30 returned HTTP 200 with nothing inside the 28 h window, so they are low-frequency blogs, not broken feeds; newest posts range from 2 days to 11 weeks old. Stale: Sean Morris ARB only.
- **Paywall drops:** run.json keeps only a per-source total, so rules are inferred by feed. Nature's JSON-LD page check dropped 10, the Substack paid rule (Latent Space) 2, and the paywalled-host list 0. HN was down all 4 runs, so the host rule got no real exercise.

36 people feeds (`docs/people-2026-09-26.md`) and the paywall filters went in on 2026-09-26. In the early source read, report per people feed: items, picks, paid_skipped, and whether it was stale; and per filter: how many items each rule dropped (run.json `sources[].paid_skipped`). **PubMed:** both saved-search feeds returned an empty channel on the evening of 09-26 after working that afternoon (NCBI throttling EVO-X2, or the saved searches expiring). If they are still empty, replace them with NCBI E-utilities (esearch + esummary for the same queries, free-full-text filter) rather than the RSS.

## 3. Dynamic fetch window (audit X3) — DONE (e44112f)

`main.fetch_window_hours`: hours since the newest earlier `*-brief.md` was written (file mtime, not `run.json`) + 0.5, clamped to 28–48 h; per-feed `window_hours` only reaches further back. Recorded as `hours_back` in `run.json`.

## 4. Feedback — 2026-09-30: 0 ticks in 4 briefs (09-27 to 09-30; 35 picks). Nothing to infer yet.

Summarize ticks so far (`~/news_agg_out/feedback/*.json`): tick rate by source, category and score. If Ian hasn't ticked anything yet, say so — don't infer.

## 5. Deliverable

A short report to Ian (numbers, recommendation, what needs his OK) and a branch with any code changes, tested. Don't deploy to production; hand Ian the commands.

## For Ian: the click check (run it yourself)

It matches brief picks against a copy of Chrome's history and prints only aggregate counts per source/category/score. Close Chrome first, then:

```
cp ~/Library/Application\ Support/Google/Chrome/Default/History /tmp/History.copy && python3 ~/projects/news_agg/docs/clicks.py /tmp/History.copy; rm /tmp/History.copy
```
