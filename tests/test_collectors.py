"""Offline parsing tests for the Tier A/B collectors.

Network fetching is not exercised here; each collector separates fetch from a
pure ``parse`` staticmethod so the parsing logic is testable against fixture
payloads without any HTTP.
"""

from __future__ import annotations

import json
import unittest

from jobradar.collectors.bluesky import BlueskyCollector
from jobradar.collectors.hackernews import HackerNewsCollector
from jobradar.collectors.reddit import RedditCollector
from jobradar.collectors.rss import RssCollector
from jobradar.collectors.scrape import ScrapeCollector


class HackerNewsParseTest(unittest.TestCase):
    def test_parse(self):
        body = json.dumps({"hits": [
            {"objectID": "42", "comment_text": "We are <b>hiring</b> a backend eng",
             "created_at_i": 1_700_000_000},
        ]}).encode()
        items = list(HackerNewsCollector.parse(body, "hn"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "42")
        self.assertNotIn("<b>", items[0].raw_text)  # HTML stripped
        self.assertTrue(items[0].url.endswith("id=42"))


class BlueskyParseTest(unittest.TestCase):
    def test_parse(self):
        body = json.dumps({"posts": [
            {"uri": "at://did:plc:abc/app.bsky.feed.post/xyz",
             "author": {"handle": "acme.bsky.social"},
             "record": {"text": "Hiring remote backend dev", "createdAt": "2026-01-01T00:00:00Z"}},
        ]}).encode()
        items = list(BlueskyCollector.parse(body, "bsky"))
        self.assertEqual(len(items), 1)
        self.assertIn("bsky.app/profile/acme.bsky.social/post/xyz", items[0].url)


class RedditParseTest(unittest.TestCase):
    def test_parse(self):
        body = json.dumps({"data": {"children": [
            {"data": {"id": "t1", "title": "[Hiring] Backend engineer",
                      "selftext": "Remote worldwide", "created_utc": 1_700_000_000,
                      "permalink": "/r/forhire/comments/t1/x"}},
        ]}}).encode()
        items = list(RedditCollector.parse(body, "reddit"))
        self.assertEqual(len(items), 1)
        self.assertIn("Backend engineer", items[0].raw_text)
        self.assertEqual(items[0].title_hint, "[Hiring] Backend engineer")


class RssParseTest(unittest.TestCase):
    def test_parse_rss(self):
        body = b"""<?xml version="1.0"?><rss><channel>
        <item><title>Backend Engineer</title>
        <description>Remote worldwide role</description>
        <link>https://jobs.example.com/1</link>
        <guid>abc-1</guid>
        <pubDate>Wed, 01 Jan 2026 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items = list(RssCollector.parse(body, "rss"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://jobs.example.com/1")
        self.assertEqual(items[0].title_hint, "Backend Engineer")
        self.assertIsNotNone(items[0].posted_at)

    def test_parse_atom(self):
        body = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Frontend Dev</title><summary>Remote</summary>
        <link href="https://x.com/2"/><id>id-2</id>
        <updated>2026-01-02T00:00:00Z</updated></entry></feed>"""
        items = list(RssCollector.parse(body, "rss"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://x.com/2")


class ScrapeParseTest(unittest.TestCase):
    def test_parse_stable_ids(self):
        body = (b"<html><body>"
                b"<article><a href='https://x/apply'>Hiring backend eng</a></article>"
                b"<article>Frontend designer wanted</article>"
                b"</body></html>")
        items = list(ScrapeCollector.parse(body, "scrape", r"<article[^>]*>(.*?)</article>"))
        self.assertEqual(len(items), 2)
        # Content-based ids are stable across re-scrapes (idempotent, FR-6).
        again = list(ScrapeCollector.parse(body, "scrape", r"<article[^>]*>(.*?)</article>"))
        self.assertEqual([i.external_id for i in items], [i.external_id for i in again])


if __name__ == "__main__":
    unittest.main()
