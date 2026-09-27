# Model check — 2026-09-26

Blind-judged comparison of triage engines on the production prompt (`prompts/triage.md` as of 2026-09-26) and the new code. Two pools: P0925 (195 articles, the audit snapshot) and P0926 (113 articles, fetched 2026-09-26 afternoon). 3 reps per Claude model, 2 for the local model. Three independent judges rated every picked article 0-3 for Ian (profile: `docs/NEXT.md`) without knowing which engine picked it; mean absolute difference between judges (0-3 scale): 0.31, 0.33, 0.50.

## Results

| arm | pool | ok | picks | mean judged score 0-3 (range over reps) | picks judged >=2 | geopolitics share | wall | tokens in/out |
|---|---|---|---|---|---|---|---|---|
| sonnet46 | P0925 | 3/3 | 12.0 | 1.64 (1.58-1.69) | 61% | 8% | 42s | 27161 / 1904 |
| sonnet46 | P0926 | 3/3 | 10.0 | 1.19 (1.10-1.27) | 20% | 23% | 31s | 15742 / 1429 |
| sonnet5 | P0925 | 3/3 | 12.0 | 1.62 (1.56-1.69) | 47% | 3% | 27s | 35376 / 2671 |
| sonnet5 | P0926 | 3/3 | 11.7 | 1.28 (1.19-1.36) | 20% | 17% | 23s | 20556 / 2275 |
| opus55 | P0925 | 3/3 | 11.7 | 1.67 (1.61-1.79) | 55% | 14% | 25s | 35378 / 2428 |
| opus55 | P0926 | 3/3 | 11.3 | 1.45 (1.36-1.52) | 50% | 26% | 21s | 20558 / 2376 |
| haiku45 | P0925 | 3/3 | 11.7 | 1.44 (1.33-1.61) | 51% | 17% | 24s | 27160 / 1831 |
| haiku45 | P0926 | 3/3 | 11.0 | 1.06 (0.94-1.17) | 15% | 33% | 19s | 15741 / 1647 |
| qwen36 | P0925 | 2/2 | 11.0 | 1.50 (1.33-1.67) | 44% | 9% | 95s | 24487 / 1762 |
| qwen36 | P0926 | 2/2 | 10.5 | 1.02 (0.93-1.11) | 8% | 24% | 71s | 14041 / 1760 |

## Overlap between runs

Overlap is measured per ARTICLE: a model URL is matched to its pool article by exact URL, then by the URL without its query string or trailing slash, then by title. The exact-URL figure is in [brackets]; it runs lower because models often strip `?utm` / `?at_medium` / `?ref=rss` from the URL. Each cell is the mean pairwise Jaccard of the kept-pick sets. Sample size is 3 reps per Claude arm and 2 for qwen36.

**P0925** (195 articles, 0 removed by dedup; 33 union items)

| arm | within-arm (rep vs rep) |
|---|---|
| sonnet46 | 0.758 [0.758] |
| sonnet5 | 0.533 [0.475] |
| opus55 | 0.846 [0.846] |
| haiku45 | 0.490 [0.490] |
| qwen36 | 0.375 [0.375] |

| between | sonnet46 | sonnet5 | opus55 | haiku45 | qwen36 |
|---|---|---|---|---|---|
| sonnet46 | — | 0.424 [0.397] | 0.481 [0.481] | 0.500 [0.500] | 0.279 [0.279] |
| sonnet5 | 0.424 [0.397] | — | 0.333 [0.333] | 0.316 [0.316] | 0.368 [0.368] |
| opus55 | 0.481 [0.481] | 0.333 [0.333] | — | 0.386 [0.386] | 0.320 [0.283] |
| haiku45 | 0.500 [0.500] | 0.316 [0.316] | 0.386 [0.386] | — | 0.308 [0.308] |
| qwen36 | 0.279 [0.279] | 0.368 [0.368] | 0.320 [0.283] | 0.308 [0.308] | — |

**P0926** (113 articles, 3 removed by dedup, so 110 were sent; 39 union items, of which 11 match no pool article)

| arm | within-arm (rep vs rep) |
|---|---|
| sonnet46 | 0.632 [0.581] |
| sonnet5 | 0.595 [0.411] |
| opus55 | 0.748 [0.748] |
| haiku45 | 0.500 [0.500] |
| qwen36 | 0.050 [0.050] |

| between | sonnet46 | sonnet5 | opus55 | haiku45 | qwen36 |
|---|---|---|---|---|---|
| sonnet46 | — | 0.537 [0.419] | 0.327 [0.201] | 0.384 [0.353] | 0.210 [0.168] |
| sonnet5 | 0.537 [0.419] | — | 0.320 [0.281] | 0.381 [0.196] | 0.269 [0.225] |
| opus55 | 0.327 [0.201] | 0.320 [0.281] | — | 0.308 [0.099] | 0.112 [0.112] |
| haiku45 | 0.384 [0.353] | 0.381 [0.196] | 0.308 [0.099] | — | 0.157 [0.067] |
| qwen36 | 0.210 [0.168] | 0.269 [0.225] | 0.112 [0.112] | 0.157 [0.067] | — |

Opus 5.5 gave the most repeatable picks on both days, followed by Sonnet 4.6. qwen36 was the least repeatable, and on P0926 its score is low mainly because it invented URLs (see notes).

## Conclusion (as written by the synthesis step)

## Model check conclusion

**Primary engine:** Opus 5.5. **Local fallback:** qwen3.6:35b-a3b, but only after triage() checks every pick's URL against the input.

**Quality.** Mean judged score on a 0–3 scale, P0925 / P0926:
- Opus 5.5: 1.67 / 1.45
- Sonnet 4.6: 1.64 / 1.19
- Sonnet 5: 1.62 / 1.28
- Haiku 4.5: 1.44 / 1.06
- qwen36: 1.50 / 1.02

On P0925 the rep ranges of the top three Claude models overlap, so that day is a tie within noise. On P0926, Opus's range (1.36–1.52) sits above Sonnet 4.6's (1.10–1.27) and only touches Sonnet 5's (1.19–1.36). On P0926, 50% of Opus's picks were judged worth reading (score 2 or more); the other models managed 8–20%.

**Other points for Opus.** It gave the most repeatable picks, with a rep-to-rep overlap of 0.85 / 0.75. It never dropped query strings from URLs, so it avoids the dedup leak. Sonnet 4.6, Sonnet 5 and Haiku did drop them, Haiku in up to 8 of 12 picks. It invented no URLs, and it took 25 s / 21 s against 42 s / 31 s for Sonnet 4.6.

**Point against Opus.** 26% of its P0926 picks were geopolitics, against 23% for Sonnet 4.6 and 17% for Sonnet 5. The prompt rewrite has to fix that; changing the model won't.

**Cost** at list price with the measured tokens, 1 run a day:

| model | per run | per year |
|---|---|---|
| Opus 5.5 | $0.13–0.19 | ~$58 |
| Sonnet 4.6 (current) | $0.07–0.11 | ~$33 |
| Sonnet 5 | $0.06–0.10 | ~$29 |
| Haiku 4.5 | $0.02–0.04 | ~$11 |

Opus would cost about $26 a year more than today. These figures already include the ~30% more input tokens that Sonnet 5 and Opus count for the same prompt. Adding sources will make the pool bigger, and cost will grow roughly in line with it.

**Reliability.**
- All 24 Claude calls succeeded on the first try, and all 4 qwen runs finished.
- qwen36 invented 7 of 12 URLs in one P0926 run, and Haiku invented 3 across two runs.
- qwen36 is 3–4× slower, at 71–95 s.
- With the URL check dropping and logging picks that match no input article, a bad qwen run gives a short brief and a logged failure instead of dead links. That is acceptable for a fallback that was used 0 times in 97 runs.
- Qwen3.8 Flash-Next was not tested. It needs about 105 GB, so GECK would have to be paused.

**Caveats.**
- Only 2 pools were tested.
- This was the current prompt, not the rewritten one.
- The judges differed from each other by 0.31–0.50 points per item on average.
- qwen has only 2 runs per pool.

Before locking this in, re-run the check on the same two saved pools after the prompt rewrite and the source changes.

Recommendation: primary=claude-opus-5-5, fallback=qwen3.6:35b-a3b, confidence=medium

## Notes

- Invented URLs: models re-picked stories from the title-only "already covered" list and made up URLs (qwen36 7/12 in one run; Haiku 3 across two). Several models also dropped `?utm`-style queries, which broke cross-day dedup. Fixed in `triage.resolve_picks`.
- Sonnet 5 and Opus 5.5 count ~30% more input tokens than Sonnet 4.6 / Haiku 4.5 for the same prompt.
- Qwen3.8 Flash-Next was not tested: ~105 GB, needs a GECK window on EVO-X2.
- Re-run this on ≥3 saved production pools after the v2 prompt and the new sources settle (`docs/NEXT.md`).
