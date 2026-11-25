"""B1: one broken source, post or message must never stop the whole check
(SRS SR-3: a failing source never halts the run; NFR-6: one malformed post
never aborts its batch)."""

import json
import logging
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar import pipeline as pipeline_mod
from jobradar.collectors.base import CollectorError, each_item
from jobradar.collectors.bluesky import BlueskyCollector
from jobradar.collectors.hackernews import HackerNewsCollector
from jobradar.collectors.reddit import RedditCollector
from jobradar.collectors.registry import REGISTRY
from jobradar.collectors.rss import RssCollector
from jobradar.collectors.scrape import ScrapeCollector
from jobradar.collectors.twitter import TwitterCollector
from jobradar.config import Config, build_sources
from jobradar.models import RawItem
from jobradar.pipeline import Pipeline
from jobradar.state import State, utcnow

HTML_ERROR_PAGE = b"<!DOCTYPE html><html><head><title>503</title></head><body>Service unavailable</body></html>"
JOB = "We're hiring a remote Python backend engineer. Apply: jobs@acme.example"


def _good(name="good", texts=(JOB,)):
    class Good:
        type = "rss"

        def __init__(self, **kw):
            pass

        def fetch(self, ctx):
            for i, t in enumerate(texts):
                yield RawItem(source=name, external_id=str(i), raw_text=t, url=f"https://x/{i}",
                              posted_at=utcnow() - timedelta(hours=1))
    return Good


def _raising(exc):
    class Broken:
        type = "rss"

        def __init__(self, **kw):
            pass

        def fetch(self, ctx):
            raise exc
            yield  # pragma: no cover
    return Broken


class _Transport:
    def __init__(self):
        self.messages = []

    def send_message(self, chat, text):
        self.messages.append(text)


class PipelineIsolationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = State(Path(tmp.name) / "jobradar.db")
        self.cfg = Config(keywords=[], remote_only=False, telegram_chat_id="1", message_interval_seconds=0)

    def run_with(self, registry, sources):
        with mock.patch.dict(REGISTRY, registry):
            transport = _Transport()
            summary = Pipeline(self.cfg, self.state, sources, transport=transport).run()
        return summary, transport

    def test_one_broken_feed_doesnt_stop_the_others(self):
        broken = _raising(ET.ParseError("no element found"))  # what used to escape and abort the run
        s, t = self.run_with({"broken": broken, "good": _good()},
                             [{"name": "broken", "type": "broken"}, {"name": "good", "type": "good"}])
        self.assertEqual(s.delivered, 1)
        self.assertEqual(s.per_source["broken"]["status"], "error")
        self.assertIn("ParseError", s.per_source["broken"]["error"])
        self.assertEqual(self.state.source("broken")["failures"], 1)

    def test_any_unexpected_error_is_isolated(self):
        roles = ["Go developer for our payments API", "Rust engineer for embedded firmware",
                 "Data scientist for the fraud models team", "iOS developer for the banking app"]
        for exc, role in zip((ValueError("bad"), KeyError("posts"), AttributeError("x"), TypeError("y")), roles):
            with self.subTest(exc=type(exc).__name__):
                text = f"We're hiring a remote {role} at company {type(exc).__name__}. Apply via our site."
                s, _ = self.run_with({"broken": _raising(exc), "good": _good(texts=(text,))},
                                     [{"name": "broken", "type": "broken"}, {"name": "good", "type": "good"}])
                self.assertEqual((s.per_source["broken"]["status"], s.delivered), ("error", 1))

    def test_unknown_source_type_is_a_clear_per_source_error(self):
        s, _ = self.run_with({"good": _good()}, [{"name": "typo", "type": "rsss"}, {"name": "good", "type": "good"}])
        self.assertEqual(s.delivered, 1)
        self.assertIn("unknown source type 'rsss'", s.per_source["typo"]["error"])

    def test_broken_scraper_is_still_auto_disabled(self):
        sources = [{"name": "scraper", "type": "broken", "tier": "B"}]
        for _ in range(3):
            s, t = self.run_with({"broken": _raising(RuntimeError("layout changed"))}, sources)
        self.assertTrue(self.state.is_disabled("scraper"))
        self.assertTrue(any("auto-disabled" in a for a in s.alerts))

    def test_one_unreadable_post_doesnt_stop_the_batch(self):
        real = pipeline_mod.classify

        def flaky(text, **kw):
            if "POISON" in text:
                raise RecursionError("pathological post")
            return real(text, **kw)

        with mock.patch.object(pipeline_mod, "classify", flaky):
            s, _ = self.run_with({"good": _good(texts=(JOB, "POISON " + JOB, JOB.replace("Python", "Go")))},
                                 [{"name": "good", "type": "good"}])
        self.assertEqual((s.delivered, s.per_source["good"]["unreadable"]), (2, 1))

    def test_a_job_card_that_cant_be_formatted_is_sent_plain(self):
        with mock.patch.object(pipeline_mod, "format_message", side_effect=ValueError("odd")):
            s, t = self.run_with({"good": _good()}, [{"name": "good", "type": "good"}])
        self.assertEqual(s.delivered, 1)
        self.assertIn("https://x/0", t.messages[0])

    def test_feed_dates_without_a_time_zone(self):
        feed = (b'<rss><channel><item><title>Hiring: remote Python engineer</title>'
                b'<description>We are hiring a backend engineer. Apply jobs@acme.example</description>'
                b'<link>https://x/1</link><pubDate>' +
                (utcnow() - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S -0000").encode() +
                b'</pubDate></item></channel></rss>')

        class FakeHttp:
            def __init__(self, *a, **kw):
                pass

            def get(self, url, **kw):
                return 200, feed

        with mock.patch.object(pipeline_mod, "HttpClient", FakeHttp):
            s, _ = self.run_with({"rss": RssCollector}, [{"name": "feed", "type": "rss", "url": "https://x/feed"}])
        self.assertEqual((s.per_source["feed"]["status"], s.delivered), ("ok", 1))


class CollectorParsingTests(unittest.TestCase):
    """Each collector: an error page becomes a clear message; a malformed
    entry is skipped while the rest still arrive."""

    def test_error_pages_get_a_clear_message(self):
        cases = [
            lambda: list(RssCollector.parse(HTML_ERROR_PAGE, "s")),
            lambda: list(BlueskyCollector.parse(HTML_ERROR_PAGE, "s")),
            lambda: list(RedditCollector.parse(HTML_ERROR_PAGE, "s")),
            lambda: list(TwitterCollector.parse(HTML_ERROR_PAGE, "s")),
            lambda: list(HackerNewsCollector._parse_comments(HTML_ERROR_PAGE, 1, "s")),
        ]
        for i, call in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(CollectorError) as cm:
                call()
            self.assertIn("web page", str(cm.exception))

    def test_bad_scrape_pattern_is_a_clear_error(self):
        with self.assertRaises(CollectorError):
            list(ScrapeCollector.parse(b"<article>x</article>", "s", "(unclosed"))

    def test_malformed_entries_are_skipped(self):
        hn = json.dumps({"hits": [
            {"parent_id": 1, "objectID": "10", "comment_text": "Acme | Backend | Remote", "created_at_i": 1700000000},
            {"parent_id": "not-a-number", "objectID": "11", "comment_text": "broken"},
            {"parent_id": 1, "objectID": "12", "comment_text": "Beta | Frontend | Remote", "created_at_i": "soon"},
            {"parent_id": 1, "objectID": "13", "comment_text": "Gamma | SRE | Remote"},
        ]}).encode()
        self.assertEqual([i.external_id for i in HackerNewsCollector._parse_comments(hn, 1, "hn")], ["10", "13"])

        reddit = json.dumps({"data": {"children": [
            {"data": {"id": "a", "title": "Hiring dev", "created_utc": 1700000000}},
            {"data": {"id": "b", "title": "Hiring dev 2", "created_utc": "yesterday"}},
            "not-an-object",
        ]}}).encode()
        self.assertEqual([i.external_id for i in RedditCollector.parse(reddit, "r")], ["a"])

    def test_each_item_helper(self):
        out = list(each_item([1, 0, 2], lambda n: RawItem(source="s", external_id=str(1 / n), raw_text="x"), "s"))
        self.assertEqual(len(out), 2)


class ConfigTests(unittest.TestCase):
    def test_incomplete_sources_blocks_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text('[[sources]]\ntype = "rss"\nurl = "https://x"\n\n'
                         '[[sources]]\nname = "ok"\ntype = "rss"\nurl = "https://y"\n', "utf-8")
            with self.assertLogs("jobradar.config", logging.WARNING):
                defs = build_sources(Config(), p)
        self.assertEqual([s["name"] for s in defs], ["ok"])


if __name__ == "__main__":
    unittest.main()
