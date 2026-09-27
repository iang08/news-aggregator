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

    def test_duplicate_links_across_feeds_dropped(self):
        a = lambda src, link: Article("t", link, "s", self.now, src, "ai", 1.0)
        kept, n = fetch.dedupe_links([a("HN", "https://x/1"), a("Reddit", "https://x/1"), a("HN", "https://x/2")])
        self.assertEqual(([x.source_name for x in kept], n), (["HN", "HN"], 1))

    def test_duplicate_link_keeps_the_informative_copy(self):
        # Live 2026-09-26: HN (listed before Quanta, weight 1.0) carries only metadata.
        link = "https://www.quantamagazine.org/gravity-seems-holographic/"
        hn = Article("Gravity", link, f"Article URL: {link} Comments URL: https://news.ycombinator.com/item?id=1 "
                     "Points: 452 # Comments: 185", self.now, "Hacker News (200+ pts)", "ai", 1.0)
        qm = Article("Gravity", link, "The biggest breakthrough in modern theoretical physics is ...",
                     self.now, "Quanta Magazine", "science", 1.5)
        kept, n = fetch.dedupe_links([hn, qm])
        self.assertEqual(([(x.source_name, x.source_category) for x in kept], n), ([("Quanta Magazine", "science")], 1))

    def test_fetch_all_drops_cross_feed_duplicates(self):
        b = {**self.src, "name": "Other", "url": "https://f/other"}
        body = rss([("t", "https://a/1", self.now - timedelta(hours=1), "s")])
        with mock.patch.object(fetch, "load_sources", return_value=[self.src, b]), \
                mock.patch.object(fetch, "_download", lambda url: (200, {}, url, body)):
            r = fetch.fetch_all_with_status(28)
        self.assertEqual((len(r.articles), r.duplicates_dropped), (1, 1))

    def test_feed_window_never_shortens_run_window(self):
        src = {**self.src, "window_hours": 12}
        two_h = self.now - timedelta(hours=20)
        with mock.patch.object(fetch, "load_sources", return_value=[src]), \
                mock.patch.object(fetch, "_download", lambda url: (200, {}, url, rss([("t", "https://a/1", two_h, "s")]))):
            r = fetch.fetch_all_with_status(28)
        self.assertEqual((len(r.articles), r.hours_back), (1, 28))

    def _feed(self, items, generator=""):
        """items: (title, link, content_html, source_href)"""
        d = self.now.strftime("%a, %d %b %Y %H:%M:%S +0000")
        gen = f"<generator>{generator}</generator>" if generator else ""
        body = "".join(
            f"<item><title>{t}</title><link>{link}</link><pubDate>{d}</pubDate>"
            f"<description>A real subtitle that is long enough to count as a summary.</description>"
            + (f"<content:encoded><![CDATA[{c}]]></content:encoded>" if c else "")
            + (f'<source url="{src}">Outlet</source>' if src else "") + "</item>"
            for t, link, c, src in items)
        return (f'<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                f"<channel><title>F</title>{gen}{body}</channel></rss>").encode()

    def test_substack_paid_posts_skipped_only_on_substack(self):
        paid = '<p>Preview text.</p><p><a href="https://x.substack.com/p/paid">Read more</a></p>'
        free = "<p>" + "Whole essay. " * 30 + "</p>"
        items = [("Paid", "https://x.substack.com/p/paid", paid, ""), ("Free", "https://x.substack.com/p/free", free, "")]
        arts, st = self.fetch_with(self._feed(items, generator="Substack"))
        self.assertEqual(([a.title for a in arts], st.paid_skipped), (["Free"], 1))
        arts, st = self.fetch_with(self._feed(items))  # a WordPress feed ending in "Read more" is left alone
        self.assertEqual((len(arts), st.paid_skipped), (2, 0))

    def test_paywalled_outlets_skipped(self):
        items = [("Econ", "https://www.economist.com/x", "", ""), ("OK", "https://ok.org/x", "", ""),
                 ("Via GN", "https://news.google.com/rss/articles/abc", "", "https://www.netdenjd.com")]
        arts, st = self.fetch_with(self._feed(items))
        self.assertEqual(([a.title for a in arts], st.paid_skipped), (["OK"], 2))

    def test_own_homepage_stubs_skipped(self):
        src = {**self.src, "url": "https://hamel.dev/index.xml"}
        items = [("Stub", "https://hamel.dev/", "", ""), ("Post", "https://hamel.dev/blog/evals", "", ""),
                 ("HN project home", "https://ollaya.dev/", "", "")]
        arts, _ = self.fetch_with(self._feed(items), src=src)
        self.assertEqual([a.title for a in arts], ["Post", "HN project home"])

    def test_page_checks_fail_closed_and_respect_the_cap(self):
        items = [(f"n{i}", f"https://note.com/u/n/n{i}", "", "") for i in range(4)] + [("other", "https://ok.org/x", "", "")]
        verdict = {"https://note.com/u/n/n0": True, "https://note.com/u/n/n1": False, "https://note.com/u/n/n2": True}
        checked = []
        def page_is_free(url):
            checked.append(url)
            if url not in verdict:
                return False  # unreachable -> fail closed
            return verdict[url]
        with mock.patch.object(fetch, "page_is_free", page_is_free):
            arts, st = self.fetch_with(self._feed(items))
            self.assertEqual(sorted(a.title for a in arts), ["n0", "n2", "other"])
            self.assertEqual(st.paid_skipped, 2)  # n1 paid, n3 unreachable
            checked.clear()
            arts, st = self.fetch_with(self._feed(items), src={**self.src, "max_items": 1, "check_access": True})
            self.assertEqual(len(arts), 1)
            self.assertLessEqual(len(checked), 3)  # stops once the cap is full

    def test_page_is_free_reads_json_ld(self):
        page = lambda text, code=200: SimpleNamespace(status_code=code, text=text)
        with mock.patch.object(fetch.httpx, "get", return_value=page('{"isAccessibleForFree":false}')):
            self.assertFalse(fetch.page_is_free("https://note.com/a/n/n1"))
        with mock.patch.object(fetch.httpx, "get", return_value=page('{"isAccessibleForFree":true}')):
            self.assertTrue(fetch.page_is_free("https://note.com/a/n/n1"))
        with mock.patch.object(fetch.httpx, "get", return_value=page("", 403)):
            self.assertFalse(fetch.page_is_free("https://note.com/a/n/n1"))
        with mock.patch.object(fetch.httpx, "get", side_effect=httpx.ConnectTimeout("t")):
            self.assertFalse(fetch.page_is_free("https://note.com/a/n/n1"))

    def test_pubmed_links_lose_per_fetch_params(self):
        self.assertEqual(fetch.canonical_link("https://pubmed.ncbi.nlm.nih.gov/41000001/?utm_source=x&ff=20260926&v=2.18"),
                         "https://pubmed.ncbi.nlm.nih.gov/41000001/")
        self.assertEqual(fetch.canonical_link("https://q/view?id=1"), "https://q/view?id=1")

    def test_japanese_fullwidth_space_kept(self):
        self.assertEqual(fetch.clean_text("7％超え　住宅購入の負担増 \n x&nbsp;&nbsp;y"), "7％超え　住宅購入の負担増 x y")

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
        picks = [pick(url=f"https://x/{i}", score=6 + i % 4) for i in range(16)]
        picks.append(pick(url="https://x/3", score=9))   # duplicate URL
        picks.append(pick(url="https://x/low", score=5))  # below MIN_SCORE
        kept, dropped = triage.finalize_picks(picks)
        self.assertEqual(len(kept), triage.MAX_PICKS)
        self.assertEqual(len({p.url for p in kept}), len(kept))
        self.assertEqual([p.interest_score for p in kept], sorted((p.interest_score for p in kept), reverse=True))
        self.assertEqual(len(kept) + len(dropped), 18)
        self.assertIn("https://x/low", [p.url for p in dropped])

    def test_score_floor_keeps_a_short_list_short(self):
        kept, dropped = triage.finalize_picks([pick(url="https://a/1", score=8), pick(url="https://a/2", score=5)])
        self.assertEqual(([p.url for p in kept], [p.url for p in dropped]), (["https://a/1"], ["https://a/2"]))


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


class ResolveEdgeTests(unittest.TestCase):
    def arts(self):
        now = datetime.now(timezone.utc)
        return [Article("Deep dive on X", "https://sub.stack/p/x?utm_source=rss&utm_medium=feed", "s", now, "HN", "ai", 1.0),
                Article("Q one", "https://q/view?id=1", "s", now, "Q", "ai", 1.0),
                Article("Q two", "https://q/view?id=2&utm_source=rss", "s", now, "Q", "ai", 1.0),
                Article("日本語", "https://jp/%E8%A8%98%E4%BA%8B", "s", now, "J", "japan", 1.0)]

    def test_query_ids_are_kept_and_tracking_dropped(self):
        ok, bad = triage.resolve_picks([pick("whatever", "https://q/view?id=2")], self.arts())
        self.assertEqual([p.url for p in ok], ["https://q/view?id=2&utm_source=rss"])
        ok, bad = triage.resolve_picks([pick("Some other headline", "https://q/view")], self.arts())
        self.assertEqual((ok, [p.url for p in bad]), ([], ["https://q/view"]))

    def test_comments_url_falls_through_to_title(self):
        ok, _ = triage.resolve_picks([pick("Deep dive on X", "https://news.ycombinator.com/item?id=41")], self.arts())
        self.assertEqual([p.url for p in ok], ["https://sub.stack/p/x?utm_source=rss&utm_medium=feed"])

    def test_percent_encoding_and_utm(self):
        ok, bad = triage.resolve_picks([pick("t", "https://jp/記事/"), pick("t2", "http://www.sub.stack/p/x")], self.arts())
        self.assertEqual((len(ok), bad), (2, []))


class PickDisplayTests(unittest.TestCase):
    def test_resolve_sets_age_and_headline_only(self):
        t = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(minute=40, second=0, microsecond=0)
        arts = [Article("Title only", "https://a/1", "", t, "S", "ai", 1.0),
                Article("Has body", "https://a/2", "x" * 200, t, "S", "ai", 1.0)]
        ok, _ = triage.resolve_picks([pick("Title only", "https://a/1"), pick("Has body", "https://a/2")], arts)
        self.assertEqual([p.headline_only for p in ok], [True, False])
        md = output.format_brief(result(ok), "2026-09-27")
        when = t.astimezone().strftime("%a %H:%M")  # a fixed local time, not "5 h ago"
        self.assertIn(f"*S* · {when} · score 7/10 · headline only", md)
        self.assertIn(f"*S* · {when} · score 7/10\n", md)
        self.assertNotIn("tags", json.dumps(triage.TRIAGE_SCHEMA))

    def test_hn_and_reddit_link_posts_are_headline_only(self):
        t = datetime.now(timezone.utc) - timedelta(hours=3)
        arts = [Article("HN", "https://a/1", "Article URL: https://x/y Comments URL: https://news.ycombinator.com/item?id=1 "
                        "Points: 452 # Comments: 185", t, "Hacker News (200+ pts)", "ai", 1.0),
                Article("Reddit", "https://a/2", "submitted by /u/someone [link] [comments]", t, "r/LocalLLaMA", "ai", 1.3)]
        ok, _ = triage.resolve_picks([pick("HN", "https://a/1"), pick("Reddit", "https://a/2")], arts)
        self.assertEqual([p.headline_only for p in ok], [True, True])

    def test_when_shows_date_for_date_only_feeds(self):  # Nature/PubMed stamp midnight
        t = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc).astimezone().replace(hour=0, minute=0, second=0)
        self.assertEqual(output._when(t.isoformat()), t.strftime("%a %-d %b"))
        self.assertEqual(output._when(""), "")

    def test_best_picks_lead_in_a_top_section(self):
        picks = [pick("mid", "https://x/m", 7, "ai"), pick("best", "https://x/b", 10, "japan"),
                 pick("good", "https://x/g", 9, "cars"), pick("jp", "https://x/j", 8, "japan")]
        md = output.format_brief(result(picks), "2026-09-27")
        self.assertLess(md.index("## ★ TOP"), md.index("## AI"))
        top = md[md.index("## ★ TOP"):md.index("## AI")]
        self.assertEqual([t for t in ("[best]", "[good]") if t in top], ["[best]", "[good]"])
        self.assertLess(top.index("[best]"), top.index("[good]"))
        self.assertEqual(md.count("[best]"), 1)  # moved, not duplicated
        self.assertNotIn("## CARS", md)          # its only pick went to the top


class HistoryTests(unittest.TestCase):
    def test_same_day_rerun_ignores_todays_brief(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "2026-09-26-brief.md").write_text("- [ ] **[Old](https://x/old)**")
            Path(d, "2026-09-27-brief.md").write_text("- [x] **[Today](https://x/today)**")
            urls, _ = triage._recent_brief_history([d], 5, exclude_date="2026-09-27")
            self.assertEqual(urls, {"https://x/old"})
            urls, _ = triage._recent_brief_history([d], 5)
            self.assertEqual(urls, {"https://x/old", "https://x/today"})

    def test_titles_with_brackets_are_remembered(self):
        # r/LocalLLaMA "[audio.cpp] ..." and HN "... [pdf]" picks: with the 28 h
        # minimum window, yesterday's picks are back in today's pool.
        with tempfile.TemporaryDirectory() as d:
            Path(d, "2026-09-26-brief.md").write_text(
                "- [ ] **[[audio.cpp] 10 hours of audio](https://r/1)**\n"
                "  *r/LocalLLaMA* · 5 h ago · score 8/10\n"
                "- [x] **[A paper [pdf]](https://h/2.pdf)**\n"
                "- **[Old format](https://o/3)**\n")
            urls, titles = triage._recent_brief_history([d], 5)
        self.assertEqual(urls, {"https://r/1", "https://h/2.pdf", "https://o/3"})
        self.assertIn("2026-09-26: [audio.cpp] 10 hours of audio", titles)


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
    request_id = "req_stream"

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
        self.assertEqual(meta["request_id"], "req_stream")  # from the stream, not the final message

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
        def claude(client, model, *a, **kw):
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

    def _run_real(self, outcomes):
        """triage() with the real _triage_via_claude over a fake client."""
        arts = [Article("T", "https://a/1?utm_source=rss", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        client = FakeClient(outcomes)
        local_json = {"summary": "l", "picks": []}
        with mock.patch.object(triage, "Anthropic", return_value=client), \
                mock.patch.object(triage, "_triage_via_local", return_value=(local_json, "{}")) as local, \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"), \
                mock.patch.object(triage.time, "sleep", lambda s: None):
            res = triage.triage(arts)
        return res, client, local

    def test_overload_on_primary_goes_to_backup(self):
        good = message('{"summary": "s", "picks": [{"title": "T", "source": "x", "category": "ai", '
                       '"url": "https://a/1", "summary": "w", "interest_score": 8, "tags": []}, '
                       '{"title": "Invented", "source": "x", "category": "ai", "url": "https://jt/fake", '
                       '"summary": "w", "interest_score": 9, "tags": []}]}')
        for failure in (api_error(529, "overloaded_error"), httpx.ReadTimeout("stall")):
            res, client, local = self._run_real([failure] * 3 + [good])
            self.assertEqual([c["model"] for c in client.calls], [triage.MODEL] * 3 + [triage.BACKUP_MODEL])
            local.assert_not_called()
            self.assertEqual(res.engine, f"claude:{triage.BACKUP_MODEL}")
            self.assertEqual(len(res.meta["primary"]["attempts"]), 3)
            # picks are resolved inside triage(): query restored, invented one dropped
            self.assertEqual([p.url for p in res.picks], ["https://a/1?utm_source=rss"])
            self.assertEqual(res.meta["unmatched_picks"], 1)

    def test_backup_gets_one_attempt_then_local(self):
        res, client, local = self._run_real([api_error(529, "overloaded_error")] * 4)
        self.assertEqual(len(client.calls), 4)  # 3 primary + 1 backup
        local.assert_called_once()
        self.assertTrue(res.engine.startswith("local:"))

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

    def test_article_order_is_shuffled_by_date(self):
        arts = [Article(f"T{i}", f"https://a/{i}", "s", datetime.now(timezone.utc), "Src", "ai", 1.0) for i in range(12)]
        seen = []
        def claude(client, model, system, user_msg, meta, **kw):
            seen.append(user_msg)
            return {"summary": "s", "picks": []}, "{}"
        with mock.patch.object(triage, "_triage_via_claude", side_effect=claude), \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"):
            triage.triage(list(arts))
            triage.triage(list(arts))
        self.assertEqual(seen[0], seen[1])  # reproducible within a day
        order = [int(l.split("/")[-1]) for l in seen[0].splitlines() if l.startswith("    URL: ")]
        self.assertEqual(sorted(order), list(range(12)))
        self.assertNotEqual(order, list(range(12)))

    def test_shuffle_seed_changes_with_the_date(self):
        arts = [Article(f"T{i}", f"https://a/{i}", "s", datetime.now(timezone.utc), "Src", "ai", 1.0) for i in range(12)]
        seen = []
        def claude(client, model, system, user_msg, meta, **kw):
            seen.append(user_msg)
            return {"summary": "s", "picks": []}, "{}"
        class Day(datetime):
            day = "2026-09-27"
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(cls.day + "T07:00:00")
        with mock.patch.object(triage, "_triage_via_claude", side_effect=claude), \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"), \
                mock.patch.object(triage, "datetime", Day):
            triage.triage(list(arts))
            Day.day = "2026-09-28"
            triage.triage(list(arts))
        self.assertNotEqual(seen[0], seen[1])

    def test_fallback_failure_keeps_both_errors(self):
        arts = [Article("T", "https://a/1", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        with mock.patch.object(triage, "_triage_via_claude", side_effect=triage.ClaudeRequestError("HTTP 400 bad param")), \
                mock.patch.object(triage, "_triage_via_local", side_effect=RuntimeError("ollama down")), \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"):
            with self.assertRaises(RuntimeError) as cm:
                triage.triage(arts)
        self.assertIn("HTTP 400 bad param", str(cm.exception))
        self.assertIn("ollama down", str(cm.exception))

    def test_disabled_fallback_names_both_claude_errors(self):
        # The catch-up runs Claude-only when the box is short of memory; the
        # failure note must say why the local model wasn't tried.
        os.environ["TRIAGE_LOCAL_FALLBACK"] = "0"
        arts = [Article("T", "https://a/1", "s", datetime.now(timezone.utc), "Src", "ai", 1.0)]
        with mock.patch.object(triage, "_triage_via_claude", side_effect=[triage.ClaudeRequestError("primary 400"),
                                                                         RuntimeError("backup down")]), \
                mock.patch.object(triage, "_triage_via_local") as local, \
                mock.patch.object(triage, "load_prompt", return_value="SYSTEM"):
            with self.assertRaises(RuntimeError) as cm:
                triage.triage(arts)
        local.assert_not_called()
        for part in ("primary 400", "backup down", "TRIAGE_LOCAL_FALLBACK=0"):
            self.assertIn(part, str(cm.exception))


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

    def test_fetch_window_follows_last_brief(self):
        now = datetime(2026, 9, 27, 7, 0)
        self.assertEqual(main.fetch_window_hours(now), (main.WINDOW_MIN_H, None))
        old = self.delivered / "2026-09-25-brief.md"
        old.write_text("x")
        os.utime(old, (now.timestamp() - 40 * 3600,) * 2)  # the last brief is 40 h old
        hours, since = main.fetch_window_hours(now)
        self.assertEqual((round(since), hours), (40, 41.0))
        os.utime(old, (now.timestamp() - 24 * 3600,) * 2)
        self.assertEqual(main.fetch_window_hours(now)[0], main.WINDOW_MIN_H)
        os.utime(old, (now.timestamp() - 90 * 3600,) * 2)
        self.assertEqual(main.fetch_window_hours(now)[0], main.WINDOW_MAX_H)
        (self.delivered / "2026-09-27-brief.md").write_text("today's, being replaced")
        self.assertEqual(main.fetch_window_hours(now)[0], main.WINDOW_MAX_H)  # today's brief is ignored

    def test_run_uses_and_records_the_fetch_window(self):
        old = self.delivered / "2025-01-01-brief.md"
        old.write_text("x")
        os.utime(old, (datetime.now().timestamp() - 40 * 3600,) * 2)
        seen = {}
        def fake_fetch(hours_back=24):
            seen["hours"] = hours_back
            f = self.fetched()
            f.hours_back, f.duplicates_dropped = hours_back, 2
            return f
        with mock.patch.object(main, "fetch_all_with_status", side_effect=fake_fetch), \
                mock.patch.object(main, "triage", return_value=result()):
            rc, _, _ = main.run()
        rec = json.loads(next((self.vault / "runs").glob("*/run.json")).read_text())
        self.assertEqual((rc, round(seen["hours"], 1), round(rec["hours_back"], 1), rec["duplicates_dropped"]), (0, 41.0, 41.0, 2))

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

    def test_unreachable_vault_still_alerts(self):
        Path(self.m.INBOX, "2026-09-27-brief.md").write_text("---\nstatus: ok\n---\n")
        real = os.makedirs
        def makedirs(path, *a, **k):
            if path == self.m.VAULT:
                raise PermissionError("TCC denied")
            return real(path, *a, **k)
        with mock.patch.object(self.m.os, "makedirs", makedirs), \
                mock.patch.object(self.m.time, "sleep", lambda s: None):
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
