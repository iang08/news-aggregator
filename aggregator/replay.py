"""
replay.py — Re-run saved production pools through triage to compare prompts or
models on exactly what production saw.

Each run record (runs/<id>/ from main.py) holds user_msg.txt: the article block
and the cross-day "already covered" block, byte for byte. Replay sends that same
message with each arm's system prompt and model, N times, and summarizes. One
run proves nothing — identical re-runs share only ~9 of 12 picks — so compare
arms against the within-arm (rep-vs-rep) overlap, on at least 3 pools.

Dry run by default (prints the plan and a cost estimate); --apply calls the API.

    python -m aggregator.replay --runs ~/news_agg_out/runs/2026-09-2* \\
        --prompt prompts/triage.md --prompt prompts/triage_v2.md --reps 3 \\
        --out ~/news_agg_audit/replay/v2 [--model claude-sonnet-4-6] [--apply]
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic

from aggregator.fetch import Article
from aggregator.triage import (
    MODEL,
    MODEL_PARAMS,
    STREAM_INACTIVITY_TIMEOUT_S,
    TriagePick,
    _triage_via_claude,
    estimate_tokens,
    finalize_picks,
    load_env,
    resolve_picks,
)

logger = logging.getLogger(__name__)

# $/MTok (input, output) — for the dry-run estimate only.
PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
EST_OUTPUT_TOKENS = 2500

# Crude signals the summary prints next to the overlap numbers; the real
# judgment is a blind read of the picks (and, over time, Ian's ticks).
VENTURE_TERMS = re.compile(
    r"kyberna|javan|jcf|japan car finder|hivemaker|autonomous research|deleuze|fitness|training|jdm",
    re.I,
)


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


_URL_LINE = re.compile(r"^    URL: (.+)$", re.M)


def sent_articles(run_dir: Path, user_msg: str) -> list[Article]:
    """The articles production actually sent: pool.json filtered to the URLs
    in user_msg (cross-day dedup had removed the rest)."""
    sent = set(_URL_LINE.findall(user_msg))
    try:
        pool = json.loads((run_dir / "pool.json").read_text())
    except (OSError, ValueError):
        return []
    return [Article(**{**a, "published": datetime.fromisoformat(a["published"])}) for a in pool if a["link"] in sent]


def run_arm(client, model: str, system_prompt: str, user_msg: str, articles: list[Article]) -> dict:
    meta: dict = {}
    try:
        parsed, raw = _triage_via_claude(client, model, system_prompt, user_msg, meta)
    except Exception as e:  # noqa: BLE001 — a failed call is a data point
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "meta": meta}
    picks = [
        TriagePick(title=p["title"], source=p["source"], category=p["category"], url=p["url"],
                   summary=p["summary"], interest_score=int(p["interest_score"]))
        for p in parsed.get("picks", [])
    ]
    picks, unmatched = resolve_picks(picks, articles)  # same as production
    kept, dropped = finalize_picks(picks)
    return {"ok": True, "summary": parsed.get("summary", ""), "picks": [asdict(p) for p in kept],
            "dropped": [asdict(p) for p in dropped], "unmatched": [asdict(p) for p in unmatched], "meta": meta}


def summarize(results: dict) -> str:
    """results[arm][run_id] = [rep dicts]"""
    lines = ["| arm | ok | picks | within-arm J | world share | venture mentions | 'directly relevant' |",
             "|---|---|---|---|---|---|---|"]
    urls = {arm: {rid: [{p["url"] for p in r.get("picks", [])} for r in reps if r["ok"]]
                  for rid, reps in runs.items()} for arm, runs in results.items()}
    for arm, runs in results.items():
        reps = [r for rs in runs.values() for r in rs]
        ok = [r for r in reps if r["ok"]]
        picks = [p for r in ok for p in r["picks"]]
        within = [jaccard(a, b) for sets in urls[arm].values() for a, b in itertools.combinations(sets, 2)]
        cats = Counter(p["category"] for p in picks)
        mentions = sum(bool(VENTURE_TERMS.search(p["summary"])) for p in picks)
        dr = sum("directly relevant" in p["summary"].lower() for p in picks)
        lines.append(
            f"| {arm} | {len(ok)}/{len(reps)} | {len(picks) / max(len(ok), 1):.1f} | "
            f"{sum(within) / len(within):.2f} | {cats['world'] / max(len(picks), 1):.0%} | "
            f"{mentions / max(len(picks), 1):.0%} | {dr} |" if within else
            f"| {arm} | {len(ok)}/{len(reps)} | {len(picks) / max(len(ok), 1):.1f} | - | - | - | {dr} |"
        )
    arms = list(results)
    if len(arms) > 1:
        lines.append("")
        lines.append("Between-arm J (mean over pools and rep pairs):")
        for a, b in itertools.combinations(arms, 2):
            js = [jaccard(x, y) for rid in urls[a] if rid in urls[b]
                  for x in urls[a][rid] for y in urls[b][rid]]
            if js:
                lines.append(f"- {a} vs {b}: {sum(js) / len(js):.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", nargs="+", required=True, help="run-record dirs (each with user_msg.txt)")
    ap.add_argument("--prompt", action="append", required=True, help="system prompt file (repeat for arms)")
    ap.add_argument("--model", action="append", help=f"model id (repeat for arms; default {MODEL})")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true", help="actually call the API")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    models = args.model or [MODEL]
    for m in models:
        if m not in MODEL_PARAMS:
            ap.error(f"unknown model {m!r}; known: {', '.join(MODEL_PARAMS)}")
    runs = []
    for d in map(Path, args.runs):
        um = d / "user_msg.txt"
        if um.is_file() and um.stat().st_size:
            text = um.read_text()
            runs.append((d.name, text, sent_articles(d, text)))
        else:
            logger.warning(f"skipping {d}: no user_msg.txt (failed or pre-2026-09-26 run)")
    if not runs:
        ap.error("no usable run records")
    prompts = {Path(p).stem: Path(p).read_text() for p in args.prompt}
    arms = [(f"{pname}@{m}", ptext, m) for (pname, ptext), m in itertools.product(prompts.items(), models)]

    calls = len(arms) * len(runs) * args.reps
    cost = sum(
        (estimate_tokens(ptext) + estimate_tokens(um)) * PRICES.get(m, (5, 25))[0] / 1e6
        + EST_OUTPUT_TOKENS * PRICES.get(m, (5, 25))[1] / 1e6
        for _, ptext, m in arms for _, um, _ in runs
    ) * args.reps
    print(f"{len(arms)} arms × {len(runs)} pools × {args.reps} reps = {calls} API calls, ~${cost:.2f}")
    for name, _, _ in arms:
        print(f"  arm {name}")
    for rid, _, arts in runs:
        print(f"  pool {rid} ({len(arts)} articles)")
    if not args.apply:
        print("dry run — add --apply to call the API")
        return 0

    load_env()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3,
                       timeout=STREAM_INACTIVITY_TIMEOUT_S)
    out = Path(args.out)
    results: dict = {}
    for (name, ptext, model), (rid, um, arts) in itertools.product(arms, runs):
        for rep in range(1, args.reps + 1):
            path = out / rid / f"{name.replace('/', '_')}_r{rep}.json"
            if path.exists():  # resumable: never pay twice for a finished call
                res = json.loads(path.read_text())
            else:
                logger.info(f"{name} on {rid} rep {rep}")
                res = run_arm(client, model, ptext, um, arts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
            results.setdefault(name, {}).setdefault(rid, []).append(res)
    summary = summarize(results)
    (out / "summary.md").write_text(summary + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
