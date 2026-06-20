"""
triage.py — Sends fetched articles to Claude, returns top picks as structured data.

Loads the prompt from prompts/triage.md, formats articles as a list,
calls the Claude API, parses the JSON response, returns a TriageResult.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from aggregator.fetch import Article

logger = logging.getLogger(__name__)

# Claude model to use. Sonnet is the right default for this kind of
# reading + structured output task. Migrated 2026-05-28 from
# claude-sonnet-4-5-20250929 to claude-sonnet-4-6 (current Sonnet) — a
# newer model routes to different inference infrastructure, which may
# resolve the mid-stream stalls we've been seeing on the 7am cron.
MODEL = "claude-sonnet-4-6"

# Maximum tokens Claude can return. ~12 picks × ~200 tokens each + headroom = 4000.
MAX_TOKENS = 4000

# Structured-output schema for the triage response. Passed as
# output_config.format so the API CONSTRAINS the model to emit valid,
# parseable JSON matching this shape — eliminating the class of failure
# that killed the 2026-06-04 brief (an article title containing literal
# quotes — `Now "Magic" Gives It Gravity` — copied verbatim into a JSON
# string without escaping, breaking json.loads).
#
# JSON-schema constraints the API allows here are limited: NO min/max on
# integers, NO minLength/maxLength on strings; every object needs
# additionalProperties:false and every property listed in `required`.
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "ai", "tech", "world", "japan", "local",
                            "science", "health", "philosophy", "cars",
                        ],
                    },
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                    "interest_score": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title", "source", "category", "url",
                    "summary", "interest_score", "tags",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "picks"],
    "additionalProperties": False,
}

# When the Anthropic API is slow, the server closes long-running streams
# mid-response with httpx.RemoteProtocolError. The SDK's max_retries does
# NOT catch this — it only retries on HTTP status codes. We retry here
# explicitly. API perf varies minute-to-minute, so a retry on a "bad
# minute" has a real chance of hitting a "good minute" (5/17 ran ~30×
# faster than 5/16 with similar inputs).
MAX_STREAM_ATTEMPTS = 3
STREAM_RETRY_BACKOFF_S = 30

# Inactivity timeout for the streaming response. Per-event instrumentation
# (5/28 logs) showed the real failure mode: the model generates partial
# output (~30-100 events over ~15-40 sec), THEN bytes stop flowing while
# the TCP connection stays open. We then sit in the for-loop waiting
# 16-25 minutes before the kernel/NAT eventually times out and httpx
# raises RemoteProtocolError. That makes the retry loop nearly useless
# (3 attempts × 17 min = 51 min before giving up).
#
# Setting the client timeout to STREAM_INACTIVITY_TIMEOUT_S means httpx
# will raise ReadTimeout if no bytes arrive for that long during the
# stream. Normal streams have events every 0.2-0.5 sec, so 60 sec of
# silence is unambiguously a stall. We then catch it and retry fast.
STREAM_INACTIVITY_TIMEOUT_S = 60.0

# Local fallback (EVO-X2 Ollama). When Claude exhausts all its retries on a
# bad-API morning (e.g. 2026-06-12: 3 hours of mid-stream stalls, no brief),
# fall back to a local model so the brief STILL ships — slower (~4 min) and
# slightly lower quality, but deterministic and immune to Anthropic-side
# stalls. Validated 2026-06-12: qwen3.6:35b-a3b returns schema-valid JSON
# with good picks on the real prompt. Disable with TRIAGE_LOCAL_FALLBACK=0.
#
# OLLAMA_HOST / TRIAGE_LOCAL_MODEL / TRIAGE_LOCAL_FALLBACK are read from the
# environment inside triage() (after load_dotenv), so .env can override them.
DEFAULT_OLLAMA_HOST = "http://evo-x2:11434"
DEFAULT_LOCAL_MODEL = "qwen3.6:35b-a3b"
LOCAL_KEEP_ALIVE = "2m"      # short — EVO-X2 is shared; don't hold RAM after the run
LOCAL_TIMEOUT_S = 600.0      # generous: a 35B model on ~25k tokens takes ~4 min
LOCAL_ATTEMPTS = 2
LOCAL_RETRY_BACKOFF_S = 15

# Cross-day dedup. The brief is otherwise stateless — each run re-triages the
# last 24h with no memory of what it featured before, so major multi-day
# stories (and any window overlap) resurface. We give triage memory by reading
# the recent brief files it already keeps: hard-exclude any article URL already
# featured (deterministic), and tell the model which topics were just covered
# so it avoids repeating them unless there's genuinely new development.
#
# History source: BRIEF_HISTORY_DIRS (os.pathsep-separated) if set, else the
# brief output dir. On EVO-X2 set it to include the delivered/ dir, since
# deliver.sh moves shipped briefs out of the output dir.
BRIEF_HISTORY_DAYS = 5
_PICK_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BRIEF_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})-brief\.md$")


def _history_dirs() -> list[str]:
    """Directories to scan for past briefs (read AFTER load_dotenv)."""
    raw = os.getenv("BRIEF_HISTORY_DIRS", "")
    if raw:
        return [d for d in raw.split(os.pathsep) if d]
    vault = os.getenv("OBSIDIAN_VAULT_PATH")
    folder = os.getenv("OBSIDIAN_BRIEF_FOLDER", "00-Inbox")
    return [os.path.join(vault, folder)] if vault else []


def _recent_brief_history(dirs: list[str], days: int) -> tuple[set[str], list[str]]:
    """Return (seen_urls, recent_picks) from the most recent `days` distinct
    brief files across `dirs`. Best-effort — any error returns ([], []) so dedup
    can never break the brief."""
    try:
        by_date: dict[str, str] = {}
        for d in dirs:
            for path in glob.glob(os.path.join(d, "*-brief.md")):
                m = _BRIEF_DATE_RE.search(os.path.basename(path))
                if m:
                    by_date.setdefault(m.group(1), path)  # one file per date
        seen_urls: set[str] = set()
        recent_picks: list[str] = []
        for date in sorted(by_date, reverse=True)[:days]:
            try:
                text = Path(by_date[date]).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for title, url in _PICK_LINK_RE.findall(text):
                seen_urls.add(url)
                recent_picks.append(f"{date}: {title}")
        return seen_urls, recent_picks
    except Exception as e:  # never let dedup break the run
        logger.warning(f"Cross-day dedup: could not read brief history ({e}); proceeding without it")
        return set(), []


@dataclass
class TriagePick:
    """A single article Claude selected as worth Ian's attention."""
    title: str
    source: str
    category: str
    url: str
    summary: str
    interest_score: int
    tags: list[str] = field(default_factory=list)


@dataclass
class TriageResult:
    """The full output of a triage run."""
    summary: str  # One-sentence theme of the day
    picks: list[TriagePick]
    article_count_in: int  # How many articles were considered
    raw_response: str  # The model's raw output, for debugging
    engine: str = "claude"  # which model produced this — "claude:..." or "local:..."


def load_prompt(path: Path = Path("prompts/triage.md")) -> str:
    """Read the system prompt from disk."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found at {path}")
    return path.read_text(encoding="utf-8")


def format_articles_for_claude(articles: list[Article]) -> str:
    """Format articles as a numbered list for Claude to read."""
    lines = []
    for i, art in enumerate(articles, start=1):
        # Trim summary to keep input token count reasonable
        summary = art.summary[:300].replace("\n", " ").strip()
        lines.append(
            f"[{i}] {art.title}\n"
            f"    Source: {art.source_name} (category: {art.source_category}, weight: {art.source_weight})\n"
            f"    URL: {art.link}\n"
            f"    Summary: {summary}\n"
        )
    return "\n".join(lines)


def _parse_triage_json(raw: str) -> dict:
    """Parse Claude's triage JSON.

    Tolerates markdown code fences defensively — with structured output
    (output_config.format) the response is bare JSON, but the fence-strip
    costs nothing and guards against a future config change. Raises
    json.JSONDecodeError on malformed input so the caller can retry.
    """
    json_text = raw.strip()
    if json_text.startswith("```"):
        # Strip ```json or ``` opening fence
        json_text = json_text.split("\n", 1)[1] if "\n" in json_text else json_text
        # Strip closing ``` fence
        if json_text.endswith("```"):
            json_text = json_text.rsplit("```", 1)[0]
        json_text = json_text.strip()
    return json.loads(json_text)


def _triage_via_claude(client: Anthropic, system_prompt: str, user_msg: str) -> tuple[dict, str]:
    """Primary path: stream from Claude with structured output, per-event
    instrumentation, and the stall/JSON retry loop. Returns (parsed, raw).
    Raises after MAX_STREAM_ATTEMPTS if the API never delivers a complete,
    parseable response — the caller decides whether to fall back."""
    raw = ""
    for attempt in range(1, MAX_STREAM_ATTEMPTS + 1):
        stream_start = time.monotonic()
        first_event_at: float | None = None
        delta_count = 0
        event_count = 0
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                # effort="low": Sonnet 4.6 defaults to effort="high" (4.5 had
                #   no effort param). For a classification/extraction task that's
                #   wasteful, and high effort means longer generation — the exact
                #   condition that triggers mid-stream stalls. low + thinking
                #   disabled matches the no-thinking behavior we had on 4.5.
                # format: structured output — the API constrains the response to
                #   valid JSON matching TRIAGE_SCHEMA, so a model-emitted title
                #   with unescaped quotes can no longer produce unparseable JSON.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA},
                },
                thinking={"type": "disabled"},
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            ) as stream:
                # Only log "structural" events (block boundaries, framing).
                # Skip content_block_delta and the SDK's synthetic "text"
                # events — those flood the log without adding signal beyond
                # the delta count we already track.
                STRUCTURAL = {
                    "message_start",
                    "content_block_start",
                    "content_block_stop",
                    "message_delta",
                    "message_stop",
                }
                for event in stream:
                    elapsed = time.monotonic() - stream_start
                    event_count += 1
                    if first_event_at is None:
                        first_event_at = elapsed
                        logger.info(f"stream first event at t+{elapsed:.2f}s: {event.type}")
                    if event.type == "content_block_delta":
                        delta_count += 1
                    elif event.type in STRUCTURAL:
                        logger.info(f"stream t+{elapsed:.2f}s: {event.type}")
                message = stream.get_final_message()
            total = time.monotonic() - stream_start
            logger.info(
                f"stream complete: {event_count} events ({delta_count} deltas) "
                f"in {total:.2f}s; first event at t+{first_event_at:.2f}s"
            )

            # Parse INSIDE the retry loop so a malformed-JSON response retries
            # instead of nuking the whole brief. With structured output this
            # should never fail, but the retry is cheap insurance.
            raw = message.content[0].text
            logger.info(f"Claude responded with {len(raw)} characters")
            return _parse_triage_json(raw), raw  # success — valid stream AND JSON
        except (httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
            # Mid-stream stall (diagnosed from 5/28 instrumented logs):
            #   - ReadTimeout: bytes stopped for STREAM_INACTIVITY_TIMEOUT_S
            #   - RemoteProtocolError: connection closed when httpx tried to read
            failure_elapsed = time.monotonic() - stream_start
            diag = (
                f"after {failure_elapsed:.2f}s; "
                f"{event_count} events received ({delta_count} deltas); "
                f"first event at t+{first_event_at:.2f}s" if first_event_at is not None
                else f"after {failure_elapsed:.2f}s; ZERO events received (no message_start)"
            )
            if attempt == MAX_STREAM_ATTEMPTS:
                logger.error(
                    f"Stream attempt {attempt}/{MAX_STREAM_ATTEMPTS} closed by server; giving up. {diag}"
                )
                raise
            logger.warning(
                f"Stream attempt {attempt}/{MAX_STREAM_ATTEMPTS} closed by server ({e}); "
                f"retrying in {STREAM_RETRY_BACKOFF_S}s. {diag}"
            )
            time.sleep(STREAM_RETRY_BACKOFF_S)
        except json.JSONDecodeError as e:
            # Structured output makes this near-impossible, but if it happens,
            # retry rather than nuking the brief. (Partial-JSON salvage was
            # considered and rejected: fragile, and structured output moots it.)
            if attempt == MAX_STREAM_ATTEMPTS:
                logger.error(
                    f"JSON parse failed on attempt {attempt}/{MAX_STREAM_ATTEMPTS} "
                    f"(despite structured output); giving up. Raw response:\n{raw}"
                )
                raise RuntimeError(f"Claude returned non-JSON output: {e}") from e
            logger.warning(
                f"JSON parse failed on attempt {attempt}/{MAX_STREAM_ATTEMPTS} ({e}); "
                f"retrying in {STREAM_RETRY_BACKOFF_S}s"
            )
            time.sleep(STREAM_RETRY_BACKOFF_S)
    raise RuntimeError("unreachable: loop returns or raises")  # for the type checker


def _triage_via_local(
    system_prompt: str, user_msg: str, ollama_host: str, local_model: str
) -> tuple[dict, str]:
    """Fallback path: EVO-X2 Ollama with the SAME prompt + schema. Slower
    (~4 min) and slightly lower quality than Claude, but deterministic and
    immune to Anthropic-side stalls. Returns (parsed, raw). Raises if the
    local box is unreachable or returns garbage after LOCAL_ATTEMPTS.

    num_ctx is sized from the real prompt — the article block is large, and
    an undersized context window silently truncates the input. keep_alive is
    pinned short because EVO-X2 is a shared box (don't hold 35B in RAM)."""
    approx_tokens = (len(system_prompt) + len(user_msg)) // 3
    num_ctx = 32768 if approx_tokens < 30000 else 49152
    logger.info(
        f"Local fallback: {local_model} @ {ollama_host} "
        f"(num_ctx={num_ctx}, ~{approx_tokens} input tokens)"
    )
    t0 = time.monotonic()
    last_err: Exception | None = None
    for attempt in range(1, LOCAL_ATTEMPTS + 1):
        try:
            resp = httpx.post(
                f"{ollama_host}/api/chat",
                json={
                    "model": local_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "format": TRIAGE_SCHEMA,  # Ollama structured output — same schema
                    "stream": False,
                    "keep_alive": LOCAL_KEEP_ALIVE,
                    "options": {"num_ctx": num_ctx, "temperature": 0.3},
                },
                timeout=LOCAL_TIMEOUT_S,
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"]
            parsed = _parse_triage_json(raw)
            logger.info(
                f"Local fallback complete in {time.monotonic() - t0:.1f}s, {len(raw)} chars"
            )
            return parsed, raw
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
            last_err = e
            if attempt == LOCAL_ATTEMPTS:
                break
            logger.warning(
                f"Local fallback attempt {attempt}/{LOCAL_ATTEMPTS} failed ({e}); "
                f"retrying in {LOCAL_RETRY_BACKOFF_S}s"
            )
            time.sleep(LOCAL_RETRY_BACKOFF_S)
    raise RuntimeError(
        f"Local fallback failed after {LOCAL_ATTEMPTS} attempts: {last_err}"
    ) from last_err


def triage(articles: list[Article]) -> TriageResult:
    """Triage articles into a brief. Tries Claude first; on a total Claude
    failure (stalls/timeouts exhausted), falls back to the EVO-X2 local model
    so the brief still ships. Set TRIAGE_LOCAL_FALLBACK=0 to disable fallback."""
    if not articles:
        raise ValueError("No articles to triage — fetch returned empty list")

    load_dotenv(override=True)  # override: shell may export an empty ANTHROPIC_API_KEY
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Check your .env file.")

    client = Anthropic(
        api_key=api_key,
        max_retries=3,
        # Used as httpx's read timeout — i.e., max gap between bytes during
        # a streaming response. See STREAM_INACTIVITY_TIMEOUT_S comment.
        timeout=STREAM_INACTIVITY_TIMEOUT_S,
    )
    system_prompt = load_prompt()

    # Cross-day dedup: drop articles already featured in recent briefs
    # (deterministic), and tell the model which topics were just covered.
    seen_urls, recent_picks = _recent_brief_history(_history_dirs(), BRIEF_HISTORY_DAYS)
    if seen_urls:
        before = len(articles)
        articles = [a for a in articles if a.link not in seen_urls]
        dropped = before - len(articles)
        if dropped:
            logger.info(
                f"Cross-day dedup: dropped {dropped} article(s) already featured "
                f"in the last {BRIEF_HISTORY_DAYS} briefs"
            )
    if not articles:
        raise ValueError("All fetched articles were already featured recently — nothing new to triage")

    article_block = format_articles_for_claude(articles)
    recent_block = ""
    if recent_picks:
        recent_block = (
            "\n\n## Already covered in the last few days' briefs\n"
            "Do NOT re-select these stories unless there is a genuinely new, material "
            "development today — and if you do, make the summary specifically about "
            "what's NEW. Otherwise prefer fresh stories.\n"
            + "\n".join(f"- {p}" for p in recent_picks)
        )

    logger.info(f"Sending {len(articles)} articles to Claude for triage...")

    user_msg = (
        f"Here are {len(articles)} articles from the last 24 hours. "
        f"Triage them per the instructions in your system prompt."
        f"{recent_block}\n\n"
        f"{article_block}"
    )

    # Fallback config (read after load_dotenv so .env can override).
    fallback_enabled = os.getenv("TRIAGE_LOCAL_FALLBACK", "1") != "0"
    ollama_host = os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    local_model = os.getenv("TRIAGE_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)

    # Claude is primary. On a TOTAL Claude failure (stalls/timeouts/parse all
    # exhausted), fall back to the local model so the brief still ships rather
    # than producing nothing — which is what happened 2026-06-12.
    engine = f"claude:{MODEL}"
    try:
        parsed, raw = _triage_via_claude(client, system_prompt, user_msg)
    except Exception as claude_err:
        if not fallback_enabled:
            raise
        logger.error(
            f"Claude triage failed ({type(claude_err).__name__}: {claude_err}). "
            f"Falling back to the local model on EVO-X2 so the brief still ships."
        )
        parsed, raw = _triage_via_local(system_prompt, user_msg, ollama_host, local_model)
        engine = f"local:{local_model}"
        logger.info(f"Brief generated via FALLBACK engine: {engine}")

    picks = [
        TriagePick(
            title=p["title"],
            source=p["source"],
            category=p["category"],
            url=p["url"],
            summary=p["summary"],
            interest_score=int(p["interest_score"]),
            tags=p.get("tags", []),
        )
        for p in parsed.get("picks", [])
    ]

    return TriageResult(
        summary=parsed.get("summary", "(no summary)"),
        picks=picks,
        article_count_in=len(articles),
        raw_response=raw,
        engine=engine,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    from aggregator.fetch import fetch_all
    
    articles = fetch_all()
    if not articles:
        print("No articles fetched. Exiting.")
        exit(1)
    
    result = triage(articles)
    
    print(f"\n=== Today's signal ===")
    print(result.summary)
    print(f"\n=== {len(result.picks)} picks from {result.article_count_in} articles ===\n")
    
    for i, pick in enumerate(result.picks, start=1):
        print(f"{i}. [{pick.category}] {pick.source} (score: {pick.interest_score}/10)")
        print(f"   {pick.title}")
        print(f"   {pick.summary}")
        print(f"   Tags: {', '.join(pick.tags)}")
        print(f"   {pick.url}")
        print()
