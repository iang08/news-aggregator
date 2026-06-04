"""
triage.py — Sends fetched articles to Claude, returns top picks as structured data.

Loads the prompt from prompts/triage.md, formats articles as a list,
calls the Claude API, parses the JSON response, returns a TriageResult.
"""

from __future__ import annotations

import json
import logging
import os
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
    raw_response: str  # Claude's raw output, for debugging


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


def triage(articles: list[Article]) -> TriageResult:
    """Send articles to Claude and return the triaged result."""
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
    article_block = format_articles_for_claude(articles)

    logger.info(f"Sending {len(articles)} articles to Claude for triage...")

    parsed: dict | None = None
    raw = ""
    for attempt in range(1, MAX_STREAM_ATTEMPTS + 1):
        # Per-event timing instrumentation. We see "200 OK then long silence
        # then RemoteProtocolError" but don't know whether tokens were
        # trickling in slowly or never arrived at all. This loop logs the
        # first event (TTFB), all non-delta events (block start/stop,
        # message_delta, etc.), and a final delta count + duration. From
        # this we can distinguish queue starvation (zero/few events) from
        # slow generation (many delta events spread over the failure window).
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
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Here are {len(articles)} articles from the last 24 hours. "
                            f"Triage them per the instructions in your system prompt.\n\n"
                            f"{article_block}"
                        ),
                    }
                ],
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
            parsed = _parse_triage_json(raw)  # raises json.JSONDecodeError
            break  # success — valid stream AND valid JSON
        except (httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
            # Two failure modes, both diagnosed from the 5/28 instrumented
            # logs as mid-stream stalls:
            #   - ReadTimeout: bytes stopped arriving for STREAM_INACTIVITY_TIMEOUT_S
            #     (our new short-circuit; replaces the 16-25 min hang)
            #   - RemoteProtocolError: the connection was already closed
            #     when httpx tried to read (server closed cleanly, or
            #     TCP/NAT eventually timed out before our inactivity timer)
            # Same fix for both: log the diagnostic counters, retry.
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
            # Structured output (output_config.format) makes this near-impossible,
            # but if the API ever returns unparseable JSON, retry rather than
            # nuking the whole brief. (Salvaging partial JSON was considered and
            # rejected: fragile, and structured output makes it unnecessary.)
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

    assert parsed is not None, "loop should have either set parsed or raised"

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
