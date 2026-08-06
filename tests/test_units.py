"""Unit tests for the domain logic."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobradar import dedup
from jobradar.classifier import classify
from jobradar.delivery import digest
from jobradar.extraction import detect_remote, extract, parse_salary
from jobradar.filters import PASS, REJECT, Settings, evaluate, match_keywords
from jobradar.models import Posting
from jobradar.state import State


def _utcnow():
    return datetime.now(timezone.utc)


class ClassifierTest(unittest.TestCase):
    def test_recall_first(self):
        self.assertTrue(classify("We're hiring a backend engineer").is_posting)
        self.assertTrue(classify("Looking for a designer, remote").is_posting)
        self.assertTrue(classify("Backend. Remote. DM me.", source_prior=1.0).is_posting)

    def test_non_posting(self):
        self.assertFalse(classify("Anyone else love this framework?").is_posting)
        self.assertFalse(classify("").is_posting)


class ExtractionTest(unittest.TestCase):
    def test_remote_detection(self):
        self.assertEqual(detect_remote("Fully remote position"), "remote")
        self.assertEqual(detect_remote("Hybrid, 3 days in office"), "hybrid")
        self.assertEqual(detect_remote("On-site in Berlin"), "onsite")
        self.assertEqual(detect_remote("Great backend role"), "unknown")

    def test_salary(self):
        s = parse_salary("Salary: $120,000 - $150,000 per year")
        self.assertEqual(s.currency, "USD")
        self.assertEqual(s.min, 120000)
        self.assertEqual(s.max, 150000)
        self.assertEqual(s.period, "year")

    def test_salary_raw_preserved(self):
        s = parse_salary("Comp: competitive, DOE + tokens")
        self.assertIsNotNone(s.raw)
        self.assertIsNone(s.min)

    def test_description_unmodified(self):
        text = "Hiring!  Backend engineer.\n\n  Remote.  "
        self.assertEqual(extract(text).description, text)

    def test_contact(self):
        self.assertEqual(extract("Hiring backend dev. Email jobs@acme.io").contact, "jobs@acme.io")


class FilterTest(unittest.TestCase):
    def _posting(self, text, is_remote=None):
        e = extract(text)
        return Posting(title=e.title, description=e.description, source="s", source_tier="A",
                       source_url="u", posted_at=_utcnow(), collected_at=_utcnow(),
                       is_remote=is_remote or e.is_remote)

    def test_keyword_variants(self):
        self.assertEqual(match_keywords("A full-stack role", ["fullstack"]), ["fullstack"])
        self.assertEqual(match_keywords("full stack dev", ["fullstack"]), ["fullstack"])
        self.assertEqual(match_keywords("FullStack", ["fullstack"]), ["fullstack"])
        self.assertEqual(match_keywords("nothing", ["fullstack"]), [])

    def test_keyword_then_remote(self):
        s = Settings(["backend"], remote_only=True)
        self.assertEqual(evaluate(self._posting("Frontend role remote"), s).decision, REJECT)
        self.assertEqual(evaluate(self._posting("Backend engineer, on-site", is_remote="onsite"), s).decision, REJECT)
        self.assertEqual(evaluate(self._posting("Backend engineer, fully remote"), s).decision, PASS)

    def test_empty_keywords_sends_everything(self):
        s = Settings([], remote_only=False)
        self.assertEqual(evaluate(self._posting("Anything at all"), s).decision, PASS)


class DedupTest(unittest.TestCase):
    def test_near_match(self):
        a = "We are hiring a senior backend engineer for our remote team"
        b = a + "!!!"
        self.assertTrue(dedup.is_duplicate(a, b))

    def test_apply_url_decisive(self):
        self.assertTrue(dedup.is_duplicate("one text here", "other words there",
                                           "https://acme.io/apply", "https://acme.io/apply"))

    def test_distinct_not_merged(self):
        self.assertFalse(dedup.is_duplicate(
            "Hiring a frontend designer in Berlin for our studio",
            "Seeking a devops engineer to manage kubernetes clusters remotely"))


class DigestTest(unittest.TestCase):
    def test_never_empty(self):
        self.assertEqual(digest.build_digest([]), [])

    def test_batched_single_message(self):
        postings = [{"title": f"Job {i}", "source": "s", "source_tier": "A", "origins": ["s"],
                     "source_url": "https://x/y", "is_remote": "remote"} for i in range(5)]
        msgs = digest.build_digest(postings)
        self.assertEqual(len(msgs), 1)
        self.assertIn("5 new", msgs[0])


class StateTest(unittest.TestCase):
    def test_roundtrip_and_atomic(self):
        p = Path(tempfile.mkdtemp()) / "s.json"
        st = State(p)
        st.mark_sent("abc")
        st.record_success("src1")
        st.save()
        st2 = State(p)
        self.assertTrue(st2.already_sent("abc"))
        self.assertIsNotNone(st2.source_last_success("src1"))

    def test_prune_bounds_size(self):
        p = Path(tempfile.mkdtemp()) / "s.json"
        st = State(p, max_sent=10)
        for i in range(50):
            st.mark_sent(f"fp{i:03d}", when=datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc))
        st.save()
        self.assertLessEqual(len(State(p).sent), 10)

    def test_corrupt_file_starts_fresh(self):
        p = Path(tempfile.mkdtemp()) / "s.json"
        p.write_text("{not valid json", "utf-8")
        st = State(p)  # must not raise
        self.assertEqual(st.sent, {})

    def test_failure_counter_and_disable(self):
        st = State(Path(tempfile.mkdtemp()) / "s.json")
        self.assertEqual(st.record_failure("x"), 1)
        self.assertEqual(st.record_failure("x"), 2)
        st.record_success("x")
        self.assertEqual(st.source("x")["failures"], 0)
        st.disable_source("x")
        self.assertTrue(st.is_disabled("x"))


if __name__ == "__main__":
    unittest.main()
