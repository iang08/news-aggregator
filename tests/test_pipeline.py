"""Offline tests for the fetch / triage / output / main / mover behaviour.

Run from the repo root:  .venv/bin/python -m unittest discover -s tests -v
No network, no API calls: HTTP and the Claude client are faked.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx

from aggregator import fetch, main, output, triage
from aggregator.fetch import Article, FeedError, SourceStatus
from aggregator.triage import TriagePick, TriageResult

REPO = Path(__file__).resolve().parent.parent


def rss(items: list[tuple[str, str, datetime, str]]) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link>"
        f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description><![CDATA[{s}]]></description></item>"
        for t, link, d, s in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{body}</channel></rss>'.encode()


def pick(title="t", url="https://x/1", score=7, category="ai") -> TriagePick:
    return TriagePick(title=title, source="S", category=category, url=url, summary="why", interest_score=score)


class EnvTestCase(unittest.TestCase):
    """Isolated env + temp vault for each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.vault = self.dir / "out"
        self.vault.mkdir()
        self.delivered = self.vault / "delivered"
        self.delivered.mkdir()
        env = {
            "OBSIDIAN_VAULT_PATH": str(self.vault),
            "OBSIDIAN_BRIEF_FOLDER": "00-Inbox",
            "BRIEF_HISTORY_DIRS": os.pathsep.join([str(self.vault / "00-Inbox"), str(self.delivered)]),
            "ANTHROPIC_API_KEY": "test-key",
            "TRIAGE_LOCAL_FALLBACK": "1",
        }
        self.env = mock.patch.dict(os.environ, env, clear=False)
        self.env.start()
        for var in ("BRIEF_OVERWRITE", "TRIAGE_MODEL", "NEWS_AGG_RUNS_DIR"):
            os.environ.pop(var, None)
        self.no_dotenv = mock.patch.object(triage, "ENV_PATH", self.dir / "no.env")
        self.no_dotenv.start()

    def tearDown(self):
        self.no_dotenv.stop()
        self.env.stop()
        self.tmp.cleanup()


# --------------------------------------------------------------------- fetch
class FetchTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.cutoff = self.now - timedelta(hours=24)
        self.src = {"name": "Feed", "url": "https://f/rss", "category": "ai", "weight": 0.8, "max_items": 0}

    def fetch_with(self, body=b"", status=200, exc=None, src=None):
        def fake(url):
            if exc:
                raise exc
            return status, {"content-type": "application/rss+xml"}, url, body
        with mock.patch.object(fetch, "_download", fake):
            return fetch.fetch_source(src or self.src, self.cutoff)

    def test_html_is_stripped_before_truncation(self):
        html_summary = '<p><a href="https://very.long/url/' + "x" * 400 + '">link</a> Real text &amp; more</p>'
        arts, st = self.fetch_with(rss([("A &amp; B", "https://a/1", self.now, html_summary)]))
        self.assertTrue(st.ok)
        self.assertEqual(arts[0].title, "A & B")
        self.assertEqual(arts[0].summary, "link Real text & more")

    def test_window_and_cap(self):
        items = [(f"n{i}", f"https://a/{i}", self.now - timedelta(hours=i), "s") for i in range(5)]
        items.append(("old", "https://a/old", self.now - timedelta(hours=30), "s"))
        arts, st = self.fetch_with(rss(items), src={**self.src, "max_items": 3})
        self.assertEqual([a.title for a in arts], ["n0", "n1", "n2"])  # newest kept
        self.assertEqual((st.articles, st.capped), (3, 2))

    def test_http_error_is_reported_not_called_malformed(self):
        arts, st = self.fetch_with(b"<html>Not found</html>", status=404)
        self.assertEqual((arts, st.ok, st.error, st.http_status), ([], False, "HTTP 404", 404))

    def test_unparseable_body(self):
        _, st = self.fetch_with(b"<html><body>hello</body></html>")
        self.assertFalse(st.ok)
        self.assertIn("not a parseable feed", st.error)

    def test_empty_but_valid_feed_is_ok(self):
        arts, st = self.fetch_with(rss([]))
        self.assertEqual((arts, st.ok), ([], True))

    def test_timeout_becomes_status(self):
        _, st = self.fetch_with(exc=FeedError("timed out (ReadTimeout)"))
        self.assertEqual((st.ok, st.error), (False, "timed out (ReadTimeout)"))

    def test_window_truncation_flag(self):
        items = [(f"n{i}", f"https://a/{i}", self.now - timedelta(minutes=30 * i), "s") for i in range(12)]
        _, st = self.fetch_with(rss(items))
        self.assertTrue(st.window_truncated)

    def test_download_deadline(self):
        class SlowStream:
            status_code, headers, url = 200, {}, "https://f/rss"
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def iter_bytes(self):
                while True:
                    yield b"x" * 10
        clock = iter(range(0, 10_000, 10))
        with mock.patch.object(fetch.httpx, "stream", lambda *a, **k: SlowStream()), \
                mock.patch.object(fetch.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(FeedError) as cm:
                fetch._download("https://f/rss")
        self.assertIn("no complete response within", str(cm.exception))


# --------------------------------------------------------------- triage: config
class ModelParamTests(EnvTestCase):
    def test_params_per_model(self):
        p = triage.request_params("claude-sonnet-4-6")
        self.assertEqual(p["thinking"], {"type": "disabled"})
        self.assertEqual(p["output_config"]["effort"], "low")
        p = triage.request_params("claude-opus-5-5")
        self.assertNotIn("thinking", p)  # disabled thinking is a 400 on Opus 5.5
        self.assertEqual(p["output_config"]["effort"], "low")
        p = triage.request_params("claude-haiku-4-5")
        self.assertNotIn("thinking", p)
        self.assertNotIn("effort", p["output_config"])  # effort is a 400 on Haiku 4.5
        for model in triage.MODEL_PARAMS:
            self.assertEqual(triage.request_params(model)["output_config"]["format"]["type"], "json_schema")

    def test_unknown_model_fails_loudly(self):
        os.environ["TRIAGE_MODEL"] = "claude-haiku-4-5-20251001"
        with self.assertRaises(ValueError):
            triage.triage_model()
        os.environ["TRIAGE_MODEL"] = "claude-sonnet-5"
        self.assertEqual(triage.triage_model(), "claude-sonnet-5")

    def test_env_file_does_not_override_caller(self):
        envfile = self.dir / "x.env"
        envfile.write_text("OBSIDIAN_VAULT_PATH=/should/not/win\nANTHROPIC_API_KEY=from-file\n")
        os.environ["ANTHROPIC_API_KEY"] = ""  # Ian's shell exports it empty
        with mock.patch.object(triage, "ENV_PATH", envfile):
            triage.load_env()
        self.assertEqual(os.environ["OBSIDIAN_VAULT_PATH"], str(self.vault))
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "from-file")


class PickTests(unittest.TestCase):
    def test_sort_dedupe_cap(self):
        picks = [pick(url=f"https://x/{i}", score=i % 10) for i in range(16)]
        picks.append(pick(url="https://x/9", score=9))  # duplicate URL
        kept, dropped = triage.finalize_picks(picks)
        self.assertEqual(len(kept), triage.MAX_PICKS)
        self.assertEqual(len({p.url for p in kept}), len(kept))
        self.assertEqual([p.interest_score for p in kept], sorted((p.interest_score for p in kept), reverse=True))
        self.assertEqual(len(kept) + len(dropped), 17)


class HistoryTests(unittest.TestCase):
    def test_same_day_rerun_ignores_todays_brief(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "2026-09-26-brief.md").write_text("- [ ] **[Old](https://x/old)**")
            Path(d, "2026-09-27-brief.md").write_text("- [x] **[Today](https://x/today)**")
            urls, _ = triage._recent_brief_history([d], 5, exclude_date="2026-09-27")
            self.assertEqual(urls, {"https://x/old"})
            urls, _ = triage._recent_brief_history([d], 5)
            self.assertEqual(urls, {"https://x/old", "https://x/today"})


class LocalCtxTests(unittest.TestCase):
    def test_japanese_counts_higher(self):
        self.assertGreater(triage.estimate_tokens("日本語" * 1000), triage.estimate_tokens("abc" * 1000) * 2)

    def test_ctx_sized_and_capped(self):
        self.assertEqual(triage.local_num_ctx("x", "y" * 3500) % 4096, 0)
        with self.assertRaises(RuntimeError):
            triage.local_num_ctx("", "日" * 70000)


# ------------------------------------------------------------ triage: Claude path
def api_error(status: int, etype: str) -> anthropic.APIStatusError:
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.APIStatusError(f"{etype}", response=resp,
                                    body={"type": "error", "error": {"type": etype, "message": etype}})


class FakeStream:
    def __init__(self, message=None, exc=None):
        self.message, self.exc = message, exc
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self):
        if self.exc:
            raise self.exc
        yield SimpleNamespace(type="message_start")
    def get_final_message(self): return self.message


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []
        self.messages = SimpleNamespace(stream=self.stream)
    def stream(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        return FakeStream(exc=o) if isinstance(o, Exception) else FakeStream(message=o)


def message(text='{"summary": "s", "picks": []}', stop="end_turn", thinking_first=False):
    blocks = [SimpleNamespace(type="thinking", thinking="")] if thinking_first else []
    blocks.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=None, _request_id="req_1")


class ClaudePathTests(unittest.TestCase):
    def setUp(self):
        self.nosleep = mock.patch.object(triage.time, "sleep", lambda s: None)
        self.nosleep.start()

    def tearDown(self):
        self.nosleep.stop()

    def call(self, outcomes, model="claude-sonnet-4-6"):
        client, meta = FakeClient(outcomes), {}
        try:
            return triage._triage_via_claude(client, model, "sys", "user", meta), client, meta
        except Exception as e:  # noqa: BLE001
            return e, client, meta

    def test_bad_request_is_not_retried(self):
        res, client, _ = self.call([api_error(400, "invalid_request_error")])
        self.assertIsInstance(res, triage.ClaudeRequestError)
        self.assertEqual(len(client.calls), 1)

    def test_overload_is_retried(self):
        (parsed, _), client, meta = self.call([api_error(529, "overloaded_error"), message()])
        self.assertEqual(parsed["summary"], "s")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(meta["request_id"], "req_1")

    def test_mid_stream_overload_with_200_status_is_retried(self):
        res, client, _ = self.call([api_error(200, "overloaded_error")] * 3)
        self.assertIsInstance(res, anthropic.APIStatusError)
        self.assertEqual(len(client.calls), 3)

    def test_stall_is_retried(self):
        (parsed, _), client, _ = self.call([httpx.ReadTimeout("stall"), message()])
        self.assertEqual(len(client.calls), 2)

    def test_truncated_response(self):
        res, client, _ = self.call([message(text='{"summ', stop="max_tokens")])
        self.assertIsInstance(res, triage.ClaudeTruncatedError)
        self.assertEqual(len(client.calls), 1)

    def test_thinking_block_first(self):
        (parsed, raw), _, _ = self.call([message(thinking_first=True)], model="claude-opus-5-5")
        self.assertEqual(parsed["picks"], [])

    def test_request_uses_model_params(self):
        _, client, _ = self.call([message()], model="claude-haiku-4-5")
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-haiku-4-5")
        self.assertNotIn("thinking", kw)
        self.assertNotIn("effort", kw["output_config"])


class TriageFallbackTests(EnvTestCase):
    def test_rejected_request_falls_back_with_reason(self):
        arts = [Article("T", "https://a/1", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        local = {"summary": "local day", "picks": [
            {"title": "T", "source": "Src", "category": "ai", "url": "https://a/1",
             "summary": "w", "interest_score": 8, "tags": []}]}
        with mock.patch.object(triage, "_triage_via_claude", side_effect=triage.ClaudeRequestError("HTTP 400 bad param")), \
                mock.patch.object(triage, "_triage_via_local", return_value=(local, json.dumps(local))), \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"):
            res = triage.triage(arts)
        self.assertTrue(res.engine.startswith("local:"))
        self.assertIn("HTTP 400 bad param", res.fallback_reason)
        self.assertEqual(len(res.picks), 1)


# ---------------------------------------------------------------------- output
def result(picks=None, engine="claude:claude-sonnet-4-6") -> TriageResult:
    return TriageResult(summary="day", picks=picks if picks is not None else [pick()], article_count_in=10,
                        raw_response="{}", engine=engine)


class OutputTests(EnvTestCase):
    def test_format(self):
        picks = [pick("low", "https://x/low", 6), pick("high", "https://x/high", 9), pick("jp", "https://x/jp", 8, "japan")]
        down = [SourceStatus(name="WW", category="local", ok=False, error="HTTP 404")]
        md = output.format_brief(result(picks), "2026-09-27", status="degraded", notices=["Dead feeds"], sources_down=down)
        self.assertTrue(md.startswith("---\ntype: news-brief\n"))
        self.assertIn("status: degraded", md)
        self.assertIn("> ⚠️ Dead feeds", md)
        self.assertLess(md.index("[high]"), md.index("[low]"))  # sorted by score
        self.assertLess(md.index("## AI"), md.index("## JAPAN"))
        self.assertIn("Sources down today: WW (HTTP 404)", md)
        # cross-day dedup and the Mac feedback harvester both still read the picks
        self.assertEqual(len(triage._PICK_LINK_RE.findall(md)), 3)
        mover = load_mover()
        self.assertEqual(sum(bool(mover._PICK_RE.match(l)) for l in md.splitlines()), 3)

    def test_overwrite_refused_then_allowed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        (self.delivered / f"{today}-brief.md").write_text("delivered, maybe ticked")
        with self.assertRaises(FileExistsError):
            output.write_brief(result())
        os.environ["BRIEF_OVERWRITE"] = "1"
        path = output.write_brief(result())
        self.assertTrue(path.exists())

    def test_success_removes_staged_failure_note(self):
        note = output.write_failure_note("boom")
        self.assertTrue(note.exists())
        output.write_brief(result())
        self.assertFalse(note.exists())

    def test_failure_note_is_not_a_brief(self):
        name = output.failure_note_name("2026-09-27")
        self.assertIsNone(triage._BRIEF_DATE_RE.search(name))
        self.assertFalse(name.endswith("-brief.md"))


# ------------------------------------------------------------------------ main
class MainTests(EnvTestCase):
    def fetched(self, failed=()):
        arts = [Article("T", "https://a/1", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        sts = [SourceStatus("Src", "ai", True, 1)] + [SourceStatus(n, "local", False, error="HTTP 404") for n in failed]
        return fetch.FetchResult(arts, sts)

    def test_ok_run_writes_brief_record_heartbeat_detail(self):
        with mock.patch.object(main, "fetch_all_with_status", return_value=self.fetched()), \
                mock.patch.object(main, "triage", return_value=result()):
            rc, status, detail = main.run()
        self.assertEqual((rc, status), (0, "ok"))
        self.assertIn("picks=1", detail)
        rec = json.loads(next((self.vault / "runs").glob("*/run.json")).read_text())
        self.assertEqual(rec["status"], "ok")
        self.assertTrue(next((self.vault / "runs").glob("*/pool.json")).exists())

    def test_fallback_is_degraded(self):
        with mock.patch.object(main, "fetch_all_with_status", return_value=self.fetched()), \
                mock.patch.object(main, "triage", return_value=result(engine="local:qwen3.6:35b-a3b")):
            rc, status, _ = main.run()
        self.assertEqual((rc, status), (0, "degraded"))
        brief = next((self.vault / "00-Inbox").glob("*-brief.md")).read_text()
        self.assertIn("Local fallback", brief)

    def test_failure_writes_note(self):
        with mock.patch.object(main, "fetch_all_with_status", return_value=self.fetched()), \
                mock.patch.object(main, "triage", side_effect=RuntimeError("API down")):
            rc, status, _ = main.run()
        self.assertEqual((rc, status), (1, "fail"))
        note = next((self.vault / "00-Inbox").glob("*-brief-FAILED.md")).read_text()
        self.assertIn("API down", note)

    def test_existing_brief_skips_before_fetching(self):
        today = datetime.now().strftime("%Y-%m-%d")
        (self.delivered / f"{today}-brief.md").write_text("x")
        with mock.patch.object(main, "fetch_all_with_status") as f:
            rc, status, _ = main.run()
        self.assertEqual((rc, status), (3, "skipped"))
        f.assert_not_called()

    def test_dead_after_three_failed_runs(self):
        runs = self.vault / "runs"
        for i, ok in enumerate([False, False]):
            d = runs / f"2026-09-2{i}_070000"
            d.mkdir(parents=True)
            (d / "run.json").write_text(json.dumps({"sources": [{"name": "WW", "ok": ok}]}))
        now = [SourceStatus("WW", "local", False, error="HTTP 404")]
        self.assertEqual([s.name for s in main.dead_sources(now, runs)], ["WW"])
        (runs / "2026-09-21_070000" / "run.json").write_text(json.dumps({"sources": [{"name": "WW", "ok": True}]}))
        self.assertEqual(main.dead_sources(now, runs), [])


# ----------------------------------------------------------------------- mover
def load_mover():
    spec = importlib.util.spec_from_file_location("mover", REPO / "scripts/mac/news_agg_move_brief.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.m = load_mover()
        for attr, sub in (("INBOX", "inbox"), ("VAULT", "vault"), ("STATE", "state")):
            (d / sub).mkdir()
            setattr(self.m, attr, str(d / sub))
        self.m.LOG = str(d / "mover.log")
        self.m.FEEDBACK = str(d / "fb/ticks.json")
        self.notes = []
        self.m.notify = lambda t, msg: self.notes.append((t, msg))

    def tearDown(self):
        self.tmp.cleanup()

    def test_moves_and_alerts(self):
        Path(self.m.INBOX, "2026-09-27-brief-FAILED.md").write_text("**Reason:** RuntimeError: API down\n")
        Path(self.m.INBOX, "2026-09-28-brief.md").write_text("---\nstatus: degraded\n---\n> ⚠️ **Local fallback** wrote this\n")
        Path(self.m.INBOX, "2026-09-29-brief.md").write_text("---\nstatus: ok\n---\n")
        self.m.move_new_files()
        self.assertEqual(sorted(os.listdir(self.m.INBOX)), [])
        self.assertEqual(sorted(t for t, _ in self.notes), ["News brief FAILED", "News brief degraded"])
        self.assertIn("API down", dict(self.notes)["News brief FAILED"])

    def test_missing_alarm_once(self):
        tz = self.m.PT
        early = datetime(2026, 9, 27, 8, 0, tzinfo=tz)
        late = datetime(2026, 9, 27, 10, 0, tzinfo=tz)
        self.m.check_missing(early)
        self.assertEqual(self.notes, [])
        self.m.check_missing(late)
        self.m.check_missing(late)
        self.assertEqual([t for t, _ in self.notes], ["News brief missing"])
        Path(self.m.VAULT, "2026-09-28-brief.md").write_text("x")
        self.m.check_missing(datetime(2026, 9, 28, 10, 0, tzinfo=tz))
        self.assertEqual(len(self.notes), 1)

    def test_feedback_harvest(self):
        Path(self.m.VAULT, "2026-09-27-brief.md").write_text(
            "- [x] **[A](https://a/1)**\n- [ ] **[B](https://a/2)**\n")
        Path(self.m.VAULT, "2026-06-01-brief.md").write_text("- [x] **[Old](https://a/0)**\n")
        self.m.harvest_feedback(datetime(2026, 9, 28, 10, 0, tzinfo=self.m.PT))
        data = json.loads(Path(self.m.FEEDBACK).read_text())
        self.assertEqual(list(data["briefs"]), ["2026-09-27"])
        self.assertEqual([p["checked"] for p in data["briefs"]["2026-09-27"]], [True, False])


if __name__ == "__main__":
    unittest.main()
