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

    def test_bad_url_does_not_stop_the_run(self):
        bad = {"name": "Bad", "url": "https://ｗｗｗ.example.jp/rss", "category": "japan", "weight": 1.0, "max_items": 0}
        good = {**self.src, "name": "Good"}
        real = fetch._download
        def download(url):
            if url == good["url"]:
                return 200, {}, url, rss([("ok", "https://a/1", self.now, "s")])
            return real(url)  # the real call raises httpx.InvalidURL before any network I/O
        with mock.patch.object(fetch, "load_sources", return_value=[bad, good]), \
                mock.patch.object(fetch, "_download", download):
            r = fetch.fetch_all_with_status(24)
        self.assertEqual([a.title for a in r.articles], ["ok"])
        self.assertEqual([s.name for s in r.failed], ["Bad"])
        self.assertIn("InvalidURL", r.failed[0].error)

    def test_plain_text_title_keeps_angle_brackets(self):
        atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>F</title>'
                b'<entry><title type="text">The &lt;dialog&gt; element</title><link href="https://a/1"/>'
                b'<updated>' + self.now.strftime("%Y-%m-%dT%H:%M:%SZ").encode() + b'</updated>'
                b'<summary type="html">&lt;p&gt;Body&lt;/p&gt;</summary></entry></feed>')
        arts, _ = self.fetch_with(atom)
        self.assertEqual((arts[0].title, arts[0].summary), ("The <dialog> element", "Body"))

    def test_empty_channel_is_an_error(self):
        # a wrong note.com ID answers 200 with an empty channel
        arts, st = self.fetch_with(rss([]))
        self.assertEqual((arts, st.ok), ([], False))
        self.assertIn("feed has no entries", st.error)

    def test_valid_feed_with_nothing_in_window_is_ok(self):
        arts, st = self.fetch_with(rss([("old", "https://a/o", self.now - timedelta(days=3), "s")]))
        self.assertEqual((arts, st.ok), ([], True))

    def test_future_dated_entries_skipped(self):
        arts, st = self.fetch_with(rss([("pinned", "https://a/p", self.now + timedelta(days=400), "s"),
                                        ("new", "https://a/n", self.now, "s")]))
        self.assertEqual(([a.title for a in arts], st.future_skipped), (["new"], 1))

    def test_short_summary_falls_back_to_content(self):
        body = ('<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                '<channel><title>T</title><item><title>Post</title><link>https://a/1</link>'
                f'<pubDate>{self.now.strftime("%a, %d %b %Y %H:%M:%S +0000")}</pubDate>'
                '<description>Subtitle only</description>'
                '<content:encoded><![CDATA[<p>' + "Real body text. " * 20 + '</p>]]></content:encoded>'
                '</item></channel></rss>').encode()
        arts, _ = self.fetch_with(body)
        self.assertTrue(arts[0].summary.startswith("Real body text."))

    def test_stale_feed_flagged(self):
        items = [(f"p{i}", f"https://a/{i}", self.now - timedelta(days=60 + i), "s") for i in range(5)]
        _, st = self.fetch_with(rss(items))
        self.assertTrue(st.ok and st.stale)
        _, st = self.fetch_with(rss([(f"p{i}", f"https://a/{i}", self.now - timedelta(days=2 + i), "s") for i in range(5)]))
        self.assertFalse(st.stale)
        _, st = self.fetch_with(rss(items), src={**self.src, "stale_days": 150})  # a quarterly journal
        self.assertFalse(st.stale)
        issue = self.now - timedelta(days=80)  # one journal issue, all entries the same date
        _, st = self.fetch_with(rss([(f"a{i}", f"https://a/{i}", issue, "s") for i in range(6)]))
        self.assertFalse(st.stale)

    def test_per_source_window_and_order(self):
        srcs = [{"name": f"S{i}", "url": f"https://f/{i}", "category": "ai", "weight": 1.0, "max_items": 0,
                 "window_hours": 96 if i == 1 else 0} for i in range(4)]
        two_days = self.now - timedelta(hours=48)
        def download(url):
            return 200, {}, url, rss([(f"t{url[-1]}", f"https://a/{url[-1]}", two_days, "s")])
        with mock.patch.object(fetch, "load_sources", return_value=srcs), mock.patch.object(fetch, "_download", download):
            r = fetch.fetch_all_with_status(24)
        self.assertEqual([a.title for a in r.articles], ["t1"])  # only the 96 h source reaches back 48 h
        self.assertEqual([s.name for s in r.sources], ["S0", "S1", "S2", "S3"])  # sources.yaml order kept

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


class ResolveTests(unittest.TestCase):
    def test_resolve_restores_links_and_drops_invented(self):
        arts = [Article("日本語の見出し", "https://jp/a?utm_source=rss", "s", datetime.now(timezone.utc), "NHK", "japan", 1.0),
                Article("Same path one", "https://q/view?id=1", "s", datetime.now(timezone.utc), "Q", "ai", 1.0),
                Article("Same path two", "https://q/view?id=2", "s", datetime.now(timezone.utc), "Q", "ai", 1.0)]
        picks = [pick("Japanese headline (translated)", "https://jp/a", 9),        # query stripped -> restored
                 pick("Same path two", "https://q/view", 8),                        # ambiguous path -> title match
                 pick("Old story", "https://www.japantimes.co.jp/2026/09/26/x", 7)]  # invented -> dropped
        ok, unmatched = triage.resolve_picks(picks, arts)
        self.assertEqual([(p.url, p.title, p.source) for p in ok],
                         [("https://jp/a?utm_source=rss", "日本語の見出し", "NHK"), ("https://q/view?id=2", "Same path two", "Q")])
        self.assertEqual([p.title for p in unmatched], ["Old story"])


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

    def test_connection_reset_is_retried(self):
        (parsed, _), client, meta = self.call([httpx.ReadError("Connection reset by peer"), message()])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(meta["attempts"][0]["error"], "ReadError")

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
    def test_primary_rejection_uses_backup_model_first(self):
        arts = [Article("T", "https://a/1", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        good = {"summary": "day", "picks": [{"title": "T", "source": "Src", "category": "ai", "url": "https://a/1",
                                             "summary": "w", "interest_score": 8, "tags": []}]}
        calls = []
        def claude(client, model, *a):
            calls.append(model)
            if model == triage.MODEL:
                raise triage.ClaudeRequestError("model refused the request")
            return good, "{}"
        with mock.patch.object(triage, "_triage_via_claude", side_effect=claude), \
                mock.patch.object(triage, "_triage_via_local") as local, \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"):
            res = triage.triage(arts)
        self.assertEqual(calls, [triage.MODEL, triage.BACKUP_MODEL])
        local.assert_not_called()
        self.assertEqual(res.engine, f"claude:{triage.BACKUP_MODEL}")
        self.assertIn("refused", res.fallback_reason)
        status, notices = main.assess(fetch.FetchResult(arts, []), res, [])
        self.assertEqual(status, "degraded")
        self.assertIn("Backup model", notices[0])

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

    def test_fallback_failure_keeps_both_errors(self):
        arts = [Article("T", "https://a/1", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        with mock.patch.object(triage, "_triage_via_claude", side_effect=triage.ClaudeRequestError("HTTP 400 bad param")), \
                mock.patch.object(triage, "_triage_via_local", side_effect=RuntimeError("ollama down")), \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"):
            with self.assertRaises(RuntimeError) as cm:
                triage.triage(arts)
        self.assertIn("HTTP 400 bad param", str(cm.exception))
        self.assertIn("ollama down", str(cm.exception))


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

    def test_bad_model_fails_before_fetching(self):
        os.environ["TRIAGE_MODEL"] = "claude-nope"
        with mock.patch.object(main, "fetch_all_with_status") as f:
            rc, status, _ = main.run()
        self.assertEqual((rc, status), (1, "fail"))
        f.assert_not_called()
        self.assertTrue(list((self.vault / "00-Inbox").glob("*-brief-FAILED.md")))

    def test_missing_vault_is_a_reported_failure(self):
        os.environ.pop("OBSIDIAN_VAULT_PATH")
        rc, status, _ = main.run()
        self.assertEqual((rc, status), (1, "fail"))

    def test_heartbeat_only_when_asked(self):
        with mock.patch("builtins.open", mock.mock_open()) as m:
            os.environ.pop("NEWS_AGG_HEARTBEAT", None)
            main._write_heartbeat("ok")
            m.assert_not_called()
            os.environ["NEWS_AGG_HEARTBEAT"] = "1"
            main._write_heartbeat("degraded", "picks=3")
            m().write.assert_called_once()
            self.assertIn(" degraded picks=3", m().write.call_args[0][0])

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
        self.m.FEEDBACK_DIR = str(d / "fb")
        self.notes = []
        self.ok = True
        def fake_notify(t, msg):
            self.notes.append((t, msg))
            return self.ok
        self.real_notify = self.m.notify
        self.m.notify = fake_notify

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

    def test_missing_alarm_once_and_only_after_wake_grace(self):
        tz = self.m.PT
        long_awake = datetime(2026, 9, 27, 6, 0, tzinfo=tz)
        self.m.check_missing(datetime(2026, 9, 27, 8, 0, tzinfo=tz), long_awake)  # before 09:30
        self.m.check_missing(datetime(2026, 9, 27, 10, 0, tzinfo=tz), datetime(2026, 9, 27, 9, 50, tzinfo=tz))  # just woke
        self.assertEqual(self.notes, [])
        self.m.check_missing(datetime(2026, 9, 27, 10, 0, tzinfo=tz), long_awake)
        self.m.check_missing(datetime(2026, 9, 27, 10, 2, tzinfo=tz), long_awake)
        self.assertEqual([t for t, _ in self.notes], ["News brief missing"])
        Path(self.m.VAULT, "2026-09-28-brief.md").write_text("x")
        self.m.check_missing(datetime(2026, 9, 28, 10, 0, tzinfo=tz), long_awake)
        self.assertEqual(len(self.notes), 1)

    def test_failed_notification_is_retried(self):
        tz = self.m.PT
        self.ok = False
        self.m.check_missing(datetime(2026, 9, 27, 10, 0, tzinfo=tz), datetime(2026, 9, 27, 6, 0, tzinfo=tz))
        self.ok = True
        self.m.check_missing(datetime(2026, 9, 27, 10, 2, tzinfo=tz), datetime(2026, 9, 27, 6, 0, tzinfo=tz))
        self.assertEqual(len(self.notes), 2)

    def test_awake_since_resets_after_sleep(self):
        tz = self.m.PT
        t0 = datetime(2026, 9, 27, 9, 0, tzinfo=tz)
        self.assertEqual(self.m.awake_since(t0), t0)
        self.assertEqual(self.m.awake_since(t0 + timedelta(minutes=2)), t0)
        t1 = t0 + timedelta(hours=2)  # slept
        self.assertEqual(self.m.awake_since(t1), t1)

    def test_notify_passes_text_as_argv(self):
        with mock.patch.object(self.m.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
            self.assertTrue(self.real_notify("News brief missing", 'No brief — "quoted" \\ back'))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[-2:], ['No brief — "quoted" \\ back', "News brief missing"])
        self.assertTrue(all("brief" not in c for c in cmd[:-2]))  # text never inside the script
        with mock.patch.object(self.m.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="err")):
            self.assertFalse(self.real_notify("t", "m"))

    def test_stuck_file_alerts(self):
        Path(self.m.INBOX, "2026-09-27-brief.md").write_text("---\nstatus: ok\n---\n")
        with mock.patch.object(self.m.shutil, "move", side_effect=PermissionError("TCC denied")), \
                mock.patch.object(self.m.time, "sleep", lambda s: None):
            self.m.move_new_files()
            self.m.move_new_files()
        self.assertEqual([t for t, _ in self.notes], ["News brief stuck"])

    def test_feedback_harvest(self):
        Path(self.m.VAULT, "2026-09-27-brief.md").write_text(
            "- [x] **[A](https://a/1)**\n- [ ] **[B](https://a/2)**\n")
        Path(self.m.VAULT, "2026-06-01-brief.md").write_text("- [x] **[Old](https://a/0)**\n")
        self.m.harvest_feedback(datetime(2026, 9, 28, 10, 0, tzinfo=self.m.PT))
        self.assertEqual(sorted(os.listdir(self.m.FEEDBACK_DIR)), ["2026-09-27.json"])
        data = json.loads(Path(self.m.FEEDBACK_DIR, "2026-09-27.json").read_text())
        self.assertEqual([p["checked"] for p in data["picks"]], [True, False])


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------- replay
class ReplayTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        from aggregator import replay
        self.replay = replay
        self.run = self.vault / "runs" / "2026-09-27_070001"
        self.run.mkdir(parents=True)
        (self.run / "user_msg.txt").write_text("Here are 1 articles...\n[1] T\n    URL: https://x/1\n")
        (self.run / "pool.json").write_text(json.dumps([
            {"title": "T", "link": "https://x/1", "summary": "s", "published": "2026-09-27T06:00:00+00:00",
             "source_name": "S", "source_category": "world", "source_weight": 1.0},
            {"title": "Deduped", "link": "https://x/2", "summary": "s", "published": "2026-09-27T06:00:00+00:00",
             "source_name": "S", "source_category": "world", "source_weight": 1.0}]))
        self.prompt = self.dir / "p.md"
        self.prompt.write_text("SYSTEM")

    def test_dry_run_calls_nothing(self):
        with mock.patch.object(self.replay, "_triage_via_claude") as call:
            rc = self.replay.main(["--runs", str(self.run), "--prompt", str(self.prompt), "--out", str(self.dir / "o")])
        self.assertEqual(rc, 0)
        call.assert_not_called()

    def test_apply_is_resumable_and_summarized(self):
        parsed = {"summary": "s", "picks": [{"title": "T", "source": "S", "category": "world", "url": "https://x/1",
                                             "summary": "directly relevant to Javan", "interest_score": 8, "tags": []}]}
        with mock.patch.object(self.replay, "_triage_via_claude", return_value=(parsed, "{}")) as call, \
                mock.patch.object(self.replay, "Anthropic"):
            args = ["--runs", str(self.run), "--prompt", str(self.prompt), "--reps", "2",
                    "--out", str(self.dir / "o"), "--apply"]
            self.replay.main(args)
            self.replay.main(args)  # second pass reuses the saved results
        self.assertEqual(call.call_count, 2)
        summary = (self.dir / "o" / "summary.md").read_text()
        self.assertIn(f"| p@{triage.MODEL} | 2/2 | 1.0 | 1.00 | 100% | 100% | 2 |", summary)
        self.assertEqual([a.link for a in self.replay.sent_articles(self.run, (self.run / "user_msg.txt").read_text())],
                         ["https://x/1"])
