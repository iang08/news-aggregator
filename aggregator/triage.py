"""
triage.py — Sends fetched articles to Claude, returns top picks as structured data.

Loads the prompt from prompts/triage.md, formats articles as a list,
calls the Claude API, parses the JSON response, returns a TriageResult.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
from anthropic import Anthropic
from dotenv import dotenv_values, load_dotenv

from aggregator.fetch import Article

logger = logging.getLogger(__name__)

# Claude model to use. Sonnet is the right default for this kind of
# reading + structured output task. Migrated 2026-05-28 from
# claude-sonnet-4-5-20250929 to claude-sonnet-4-6 (current Sonnet) — a
# newer model routes to different inference infrastructure, which may
# resolve the mid-stream stalls we've been seeing on the 7am cron.
MODEL = "claude-sonnet-4-6"

# Request parameters differ by model, and a wrong one is a 400 — on 2026-09-25
# Haiku 4.5 rejected `effort` and Opus 5.5 rejected disabled thinking. Before
# this table, a model swap would have silently turned every brief into a
# local-fallback brief. A model missing here fails at startup instead.
#   effort:   output_config.effort, or None to omit (Haiku 4.5 has none)
#   thinking: the `thinking` param, or None to omit (Opus 5.5 can't disable it;
#             omitting = adaptive, kept short by effort=low; Haiku 4.5 omitted =
#             no thinking)
MODEL_PARAMS: dict[str, dict] = {
    "claude-sonnet-4-6": {"effort": "low", "thinking": {"type": "disabled"}},
    "claude-sonnet-5": {"effort": "low", "thinking": {"type": "disabled"}},
    "claude-opus-5-5": {"effort": "low", "thinking": None},
    "claude-haiku-4-5": {"effort": None, "thinking": None},
}

# Maximum tokens Claude can return. ~12 picks × ~300 tokens is ~4k, but Sonnet 5
# returned 14 picks and Opus 5.5 spends some of this on thinking; a response cut
# at the cap is unparseable, and unused headroom costs nothing.
MAX_TOKENS = 16000

# The brief shows at most this many picks, best first. The model fills ~12
# slots whatever the day (87/99 briefs had exactly 12, 15% of picks scored <= 6),
# and Sonnet 5 returned 14, so the cap lives in code, not in the prompt.
MAX_PICKS = 12

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

# API errors worth retrying: rate limits, overload and server errors. Anything
# else (400 bad parameter, 401/403 key, 404 model) is a bug in the request —
# retrying can't fix it, so it goes straight to the fallback with a loud reason.
# An overload that arrives mid-stream surfaces as an SSE error event, so the
# error `type` is checked as well as the HTTP status.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
RETRYABLE_ERROR_TYPES = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}


class ClaudeRequestError(RuntimeError):
    """Claude rejected the request itself (4xx, refusal) — retrying won't help."""


class ClaudeTruncatedError(RuntimeError):
    """The response hit max_tokens, so its JSON is incomplete."""

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
# Context window: sized from the real prompt plus room for the answer, capped.
# Japanese text is ~1 token per character, so a chars/3 estimate undercounts it,
# and an undersized num_ctx makes Ollama silently drop the start of the prompt.
LOCAL_OUTPUT_ROOM = 6000
LOCAL_MAX_CTX = 65536
# Don't load a second ~23 GB model onto the shared box when it's already tight
# (GECK's own 35B runs there too). Checked only for a local Ollama.
LOCAL_MIN_AVAIL_GB = 30

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

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> None:
    """Load .env WITHOUT overriding variables the caller set on purpose — a dry
    run's OBSIDIAN_VAULT_PATH or an experiment's BRIEF_HISTORY_DIRS used to be
    silently replaced by .env values (override=True), sending test briefs to the
    real inbox. The one exception is the API key: Ian's interactive shell exports
    ANTHROPIC_API_KEY= (empty), which must not shadow the .env key."""
    load_dotenv(ENV_PATH, override=False)
    if not os.getenv("ANTHROPIC_API_KEY"):
        key = dotenv_values(ENV_PATH).get("ANTHROPIC_API_KEY")
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key


def triage_model() -> str:
    """The Claude model to use: TRIAGE_MODEL from the environment, else MODEL."""
    model = os.getenv("TRIAGE_MODEL") or MODEL
    if model not in MODEL_PARAMS:
        raise ValueError(
            f"TRIAGE_MODEL={model!r} has no entry in MODEL_PARAMS; add its "
            f"effort/thinking settings before using it (known: {', '.join(MODEL_PARAMS)})"
        )
    return model


def request_params(model: str) -> dict:
    """Keyword arguments for messages.stream() that depend on the model."""
    cfg = MODEL_PARAMS[model]
    output_config: dict = {"format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}}
    if cfg["effort"]:
        output_config["effort"] = cfg["effort"]
    params: dict = {"output_config": output_config}
    if cfg["thinking"] is not None:
        params["thinking"] = cfg["thinking"]
    return params


def _history_dirs() -> list[str]:
    """Directories to scan for past briefs (read AFTER load_dotenv)."""
    raw = os.getenv("BRIEF_HISTORY_DIRS", "")
    if raw:
        return [d for d in raw.split(os.pathsep) if d]
    vault = os.getenv("OBSIDIAN_VAULT_PATH")
    folder = os.getenv("OBSIDIAN_BRIEF_FOLDER", "00-Inbox")
    return [os.path.join(vault, folder)] if vault else []


def _recent_brief_history(
    dirs: list[str], days: int, exclude_date: str | None = None
) -> tuple[set[str], list[str]]:
    """Return (seen_urls, recent_picks) from the most recent `days` distinct
    brief files across `dirs`, skipping `exclude_date` — a same-day re-run must
    not dedup against the very brief it is replacing (it dropped 12/12 of the
    morning's picks). Best-effort — any error returns ([], []) so dedup can
    never break the brief."""
    try:
        by_date: dict[str, str] = {}
        for d in dirs:
            for path in glob.glob(os.path.join(d, "*-brief.md")):
                m = _BRIEF_DATE_RE.search(os.path.basename(path))
                if m and m.group(1) != exclude_date:
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
    # Everything needed to replay or audit the run (saved by main.py):
    dropped: list[TriagePick] = field(default_factory=list)  # over MAX_PICKS or duplicate URL
    fallback_reason: str = ""   # why Claude wasn't used, when engine is local
    system_prompt: str = ""
    user_msg: str = ""
    meta: dict = field(default_factory=dict)  # model, params, usage, stop_reason, request_id, attempts


def load_prompt(path: Path = Path("prompts/triage.md")) -> str:
    """Read the system prompt from disk."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found at {path}")
    return path.read_text(encoding="utf-8")


def format_articles_for_claude(articles: list[Article]) -> str:
    """Format articles as a numbered list for Claude to read."""
    lines = []
    for i, art in enumerate(articles, start=1):
        # Trim summary to keep input token count reasonable (fetch.py already
        # stripped the HTML, so these 300 chars are all readable text)
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


def _api_error_type(e: anthropic.APIStatusError) -> str:
    """The Anthropic error `type` (e.g. "overloaded_error") from an API error."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type"):
            return str(err["type"])
        if body.get("type") and body.get("type") != "error":
            return str(body["type"])
    return ""


def _is_retryable(e: anthropic.APIStatusError) -> bool:
    return e.status_code in RETRYABLE_STATUS or _api_error_type(e) in RETRYABLE_ERROR_TYPES


def _message_text(message) -> str:
    """Concatenate the text blocks. The first block isn't always text — with
    thinking on (Opus 5.5 can't turn it off) it's a thinking block."""
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def _usage_dict(message) -> dict:
    usage = getattr(message, "usage", None)
    try:
        return usage.model_dump(exclude_none=True) if usage is not None else {}
    except Exception:  # noqa: BLE001 — usage is diagnostics only
        return {}


def _triage_via_claude(
    client: Anthropic, model: str, system_prompt: str, user_msg: str, meta: dict
) -> tuple[dict, str]:
    """Primary path: stream from Claude with structured output, per-event
    instrumentation, and the stall/overload/JSON retry loop. Returns (parsed,
    raw) and fills `meta` (usage, stop_reason, request_id, attempts). Raises
    ClaudeRequestError / ClaudeTruncatedError at once for problems a retry
    can't fix, and re-raises after MAX_STREAM_ATTEMPTS otherwise — the caller
    decides whether to fall back."""
    raw = ""
    attempts: list[dict] = meta.setdefault("attempts", [])
    for attempt in range(1, MAX_STREAM_ATTEMPTS + 1):
        stream_start = time.monotonic()
        first_event_at: float | None = None
        delta_count = 0
        event_count = 0
        try:
            with client.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                # Per-model effort/thinking + structured output — see MODEL_PARAMS.
                # effort="low": for a classification/extraction task high effort
                #   is wasteful and means longer generation — the exact condition
                #   that triggers mid-stream stalls.
                # format: structured output — the API constrains the response to
                #   valid JSON matching TRIAGE_SCHEMA, so a model-emitted title
                #   with unescaped quotes can no longer produce unparseable JSON.
                **request_params(model),
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
            meta.update(
                usage=_usage_dict(message),
                stop_reason=message.stop_reason,
                request_id=getattr(message, "_request_id", None),
            )
            attempts.append({"attempt": attempt, "seconds": round(total, 1), "error": None})
            logger.info(f"usage: {meta['usage']}; stop_reason={message.stop_reason}; request_id={meta['request_id']}")

            if message.stop_reason == "max_tokens":
                raise ClaudeTruncatedError(
                    f"response hit max_tokens={MAX_TOKENS}; the JSON is incomplete"
                )
            if message.stop_reason == "refusal":
                raise ClaudeRequestError(f"model refused the request: {getattr(message, 'stop_details', None)}")

            # Parse INSIDE the retry loop so a malformed-JSON response retries
            # instead of nuking the whole brief. With structured output this
            # should never fail, but the retry is cheap insurance.
            raw = _message_text(message)
            logger.info(f"Claude responded with {len(raw)} characters")
            return _parse_triage_json(raw), raw  # success — valid stream AND JSON
        except (ClaudeRequestError, ClaudeTruncatedError):
            raise
        except anthropic.APIStatusError as e:
            etype = _api_error_type(e)
            attempts.append({"attempt": attempt, "seconds": round(time.monotonic() - stream_start, 1),
                             "error": f"HTTP {e.status_code} {etype}".strip()})
            if not _is_retryable(e):
                raise ClaudeRequestError(
                    f"Claude rejected the request (HTTP {e.status_code} {etype}): {getattr(e, 'message', e)}"
                ) from e
            if attempt == MAX_STREAM_ATTEMPTS:
                logger.error(f"API error on attempt {attempt}/{MAX_STREAM_ATTEMPTS} (HTTP {e.status_code} {etype}); giving up")
                raise
            logger.warning(
                f"API error on attempt {attempt}/{MAX_STREAM_ATTEMPTS} (HTTP {e.status_code} {etype}); "
                f"retrying in {STREAM_RETRY_BACKOFF_S}s"
            )
            time.sleep(STREAM_RETRY_BACKOFF_S)
        except (httpx.TransportError, anthropic.APIConnectionError) as e:
            # Mid-stream stall (diagnosed from 5/28 instrumented logs):
            #   - ReadTimeout: bytes stopped for STREAM_INACTIVITY_TIMEOUT_S
            #   - RemoteProtocolError: connection closed when httpx tried to read
            #   - ReadError etc.: the connection was reset mid-stream
            # APIConnectionError/APITimeoutError: the SDK's own connection
            # retries ran out before the stream started.
            failure_elapsed = time.monotonic() - stream_start
            diag = (
                f"after {failure_elapsed:.2f}s; "
                f"{event_count} events received ({delta_count} deltas); "
                f"first event at t+{first_event_at:.2f}s" if first_event_at is not None
                else f"after {failure_elapsed:.2f}s; ZERO events received (no message_start)"
            )
            attempts.append({"attempt": attempt, "seconds": round(failure_elapsed, 1),
                             "error": f"{type(e).__name__}"})
            if attempt == MAX_STREAM_ATTEMPTS:
                logger.error(
                    f"Stream attempt {attempt}/{MAX_STREAM_ATTEMPTS} failed ({type(e).__name__}); giving up. {diag}"
                )
                raise
            logger.warning(
                f"Stream attempt {attempt}/{MAX_STREAM_ATTEMPTS} failed ({type(e).__name__}: {e}); "
                f"retrying in {STREAM_RETRY_BACKOFF_S}s. {diag}"
            )
            time.sleep(STREAM_RETRY_BACKOFF_S)
        except json.JSONDecodeError as e:
            # Structured output makes this near-impossible, but if it happens,
            # retry rather than nuking the brief. (Partial-JSON salvage was
            # considered and rejected: fragile, and structured output moots it.)
            attempts[-1]["error"] = "JSONDecodeError"
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


def estimate_tokens(text: str) -> int:
    """Rough token count: ~3.5 chars per token for ASCII, ~1 token per char for
    everything else (Japanese). Deliberately errs high."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return int((len(text) - non_ascii) / 3.5 + non_ascii * 1.1)


def local_num_ctx(system_prompt: str, user_msg: str) -> int:
    """num_ctx for Ollama: prompt estimate + answer room, rounded up to 4k.
    Raises if even LOCAL_MAX_CTX can't hold it — a silently truncated prompt
    is worse than a loud failure."""
    need = estimate_tokens(system_prompt) + estimate_tokens(user_msg) + LOCAL_OUTPUT_ROOM
    if need > LOCAL_MAX_CTX:
        raise RuntimeError(f"prompt needs ~{need} tokens of context; the local fallback allows {LOCAL_MAX_CTX}")
    return max(16384, math.ceil(need / 4096) * 4096)


def _local_memory_guard(ollama_host: str, local_model: str) -> None:
    """Refuse to load the fallback model onto the shared box when memory is
    tight, unless it is already loaded. Only applies to a local Ollama on Linux."""
    if not any(h in ollama_host for h in ("localhost", "127.0.0.1")):
        return
    try:
        with open("/proc/meminfo") as f:
            avail_kb = next(int(line.split()[1]) for line in f if line.startswith("MemAvailable:"))
    except (OSError, StopIteration, ValueError):
        return
    avail_gb = avail_kb / 1024 / 1024
    if avail_gb >= LOCAL_MIN_AVAIL_GB:
        return
    try:
        loaded = [m.get("name", "") for m in httpx.get(f"{ollama_host}/api/ps", timeout=10).json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        loaded = []
    if local_model in loaded:
        return
    raise RuntimeError(
        f"only {avail_gb:.0f} GB available on the box; not loading {local_model} "
        f"(needs >= {LOCAL_MIN_AVAIL_GB} GB free)"
    )


def _triage_via_local(
    system_prompt: str, user_msg: str, ollama_host: str, local_model: str, meta: dict
) -> tuple[dict, str]:
    """Fallback path: EVO-X2 Ollama with the SAME prompt + schema. Slower
    (~4 min) and slightly lower quality than Claude, but deterministic and
    immune to Anthropic-side stalls. Returns (parsed, raw). Raises if the
    local box is unreachable or returns garbage after LOCAL_ATTEMPTS.

    num_ctx is sized from the real prompt — the article block is large, and
    an undersized context window silently truncates the input. keep_alive is
    pinned short because EVO-X2 is a shared box (don't hold 35B in RAM).
    think=False: Qwen3.6 reasons by default, which only adds minutes here."""
    num_ctx = local_num_ctx(system_prompt, user_msg)
    _local_memory_guard(ollama_host, local_model)
    logger.info(f"Local fallback: {local_model} @ {ollama_host} (num_ctx={num_ctx})")
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
                    "think": False,
                    "keep_alive": LOCAL_KEEP_ALIVE,
                    "options": {"num_ctx": num_ctx, "temperature": 0.3},
                },
                timeout=LOCAL_TIMEOUT_S,
            )
            resp.raise_for_status()
            body = resp.json()
            raw = body["message"]["content"]
            parsed = _parse_triage_json(raw)
            meta.update(
                local_model=local_model,
                num_ctx=num_ctx,
                usage={"prompt_eval_count": body.get("prompt_eval_count"), "eval_count": body.get("eval_count")},
                local_seconds=round(time.monotonic() - t0, 1),
            )
            if (body.get("prompt_eval_count") or 0) >= num_ctx:
                logger.warning(f"Local fallback: prompt filled num_ctx={num_ctx}; input may have been truncated")
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


def finalize_picks(picks: list[TriagePick]) -> tuple[list[TriagePick], list[TriagePick]]:
    """Best first, one entry per URL, at most MAX_PICKS. Returns (kept, dropped)."""
    seen: set[str] = set()
    kept: list[TriagePick] = []
    dropped: list[TriagePick] = []
    for p in sorted(picks, key=lambda p: -p.interest_score):  # stable: ties keep model order
        if p.url in seen or len(kept) >= MAX_PICKS:
            dropped.append(p)
        else:
            seen.add(p.url)
            kept.append(p)
    return kept, dropped


def triage(articles: list[Article]) -> TriageResult:
    """Triage articles into a brief. Tries Claude first; on a total Claude
    failure (stalls/timeouts exhausted, or a request Claude rejects), falls back
    to the EVO-X2 local model so the brief still ships — with the reason
    recorded so the brief and the heartbeat say so. Set
    TRIAGE_LOCAL_FALLBACK=0 to disable fallback."""
    if not articles:
        raise ValueError("No articles to triage — fetch returned empty list")

    load_env()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Check your .env file.")
    model = triage_model()

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
    today = datetime.now().strftime("%Y-%m-%d")
    seen_urls, recent_picks = _recent_brief_history(_history_dirs(), BRIEF_HISTORY_DAYS, exclude_date=today)
    dropped = 0
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

    logger.info(f"Sending {len(articles)} articles to Claude ({model}) for triage...")

    user_msg = (
        f"Here are {len(articles)} articles from the last 24 hours. "
        f"Triage them per the instructions in your system prompt."
        f"{recent_block}\n\n"
        f"{article_block}"
    )

    # Fallback config (read after load_env so .env can supply it).
    fallback_enabled = os.getenv("TRIAGE_LOCAL_FALLBACK", "1") != "0"
    ollama_host = os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    local_model = os.getenv("TRIAGE_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)

    meta: dict = {
        "model": model,
        "params": {k: v for k, v in request_params(model).items() if k != "output_config"}
        | {"effort": MODEL_PARAMS[model]["effort"], "max_tokens": MAX_TOKENS},
        "prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "articles_after_dedup": len(articles),
        "dedup_dropped": dropped,
    }

    # Claude is primary. On a TOTAL Claude failure, fall back to the local model
    # so the brief still ships rather than producing nothing — which is what
    # happened 2026-06-12. The reason travels with the result: a fallback brief
    # caused by a bad request (e.g. a model swap) must not look like a bad
    # Anthropic morning.
    engine = f"claude:{model}"
    fallback_reason = ""
    try:
        parsed, raw = _triage_via_claude(client, model, system_prompt, user_msg, meta)
    except Exception as claude_err:
        fallback_reason = f"{type(claude_err).__name__}: {claude_err}"[:300]
        if not fallback_enabled:
            raise
        logger.error(
            f"Claude triage failed ({fallback_reason}). "
            f"Falling back to the local model so the brief still ships."
        )
        try:
            parsed, raw = _triage_via_local(system_prompt, user_msg, ollama_host, local_model, meta)
        except Exception as local_err:
            # Keep BOTH causes: the failure note should say why Claude failed too.
            raise RuntimeError(
                f"Claude failed ({fallback_reason[:200]}); local fallback failed "
                f"({type(local_err).__name__}: {str(local_err)[:200]})"
            ) from local_err
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
    kept, dropped_picks = finalize_picks(picks)
    if dropped_picks:
        logger.info(
            f"Picks: kept {len(kept)}, dropped {len(dropped_picks)} "
            f"(scores {', '.join(str(p.interest_score) for p in dropped_picks)})"
        )

    return TriageResult(
        summary=parsed.get("summary", "(no summary)"),
        picks=kept,
        article_count_in=len(articles),
        raw_response=raw,
        engine=engine,
        dropped=dropped_picks,
        fallback_reason=fallback_reason,
        system_prompt=system_prompt,
        user_msg=user_msg,
        meta=meta,
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
