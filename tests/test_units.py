"""Unit tests for the domain logic behind the acceptance criteria."""

from __future__ import annotations

import unittest

from jobradar import dedup, geo
from jobradar.classifier import classify
from jobradar.delivery import digest
from jobradar.delivery.telegram import DeliveryService
from jobradar.extraction import detect_remote, extract, parse_salary
from jobradar.filters import PASS, REJECT, REVIEW, UserSettings, evaluate, match_keywords
from jobradar.models import Posting
from jobradar.db import utcnow
from tests.helpers import FakeTransport, make_service


class GeoTest(unittest.TestCase):
    def test_worldwide_terms(self):
        for t in ["remote, worldwide", "Fully remote (global)", "hire from anywhere",
                  "Location independent role"]:
            self.assertTrue(geo.is_worldwide(t), t)
        self.assertFalse(geo.is_worldwide("Remote within Germany"))

    def test_country_extraction_and_normalisation(self):
        self.assertEqual(geo.normalise_country_list(["Serbia", "germany", "US"]),
                         ["RS", "DE", "US"])
        self.assertIn("DE", geo.extract_hiring_countries("Based in Germany, EU timezone"))

    def test_bloc_expansion(self):
        codes = geo.normalise_country_list(["EU"])
        self.assertIn("DE", codes)
        self.assertIn("FR", codes)

    def test_eligibility_worldwide_passes_for_everyone(self):
        r = geo.check_eligibility([], True, ["RS"])
        self.assertTrue(r.passed)
        self.assertFalse(r.undetermined)

    def test_eligibility_intersection(self):
        self.assertTrue(geo.check_eligibility(["DE", "US"], False, ["RS", "DE"]).passed)
        self.assertFalse(geo.check_eligibility(["US"], False, ["RS"]).passed)

    def test_eligibility_undetermined_routes_to_review(self):
        r = geo.check_eligibility([], False, ["RS"])
        self.assertTrue(r.undetermined)


class ClassifierTest(unittest.TestCase):
    def test_recall_first_keeps_borderline(self):
        self.assertTrue(classify("We're hiring a backend engineer").is_posting)
        self.assertTrue(classify("Looking for a designer, remote").is_posting)

    def test_source_prior_keeps_short_posts(self):
        # A terse comment in a jobs-only thread should still count with a prior.
        weak = "Backend. Remote. DM me."
        self.assertTrue(classify(weak, source_prior=1.0).is_posting)

    def test_non_posting_rejected(self):
        self.assertFalse(classify("Anyone else love this framework?").is_posting)
        self.assertFalse(classify("").is_posting)


class ExtractionTest(unittest.TestCase):
    def test_remote_detection(self):
        self.assertEqual(detect_remote("Fully remote position"), "remote")
        self.assertEqual(detect_remote("Hybrid, 3 days in office"), "hybrid")
        self.assertEqual(detect_remote("On-site in Berlin"), "onsite")
        self.assertEqual(detect_remote("Great backend role"), "unknown")

    def test_salary_parsing(self):
        s = parse_salary("Salary: $120,000 - $150,000 per year")
        self.assertEqual(s.currency, "USD")
        self.assertEqual(s.min, 120000)
        self.assertEqual(s.max, 150000)
        self.assertEqual(s.period, "year")

    def test_salary_raw_preserved_when_unparseable(self):
        s = parse_salary("Comp: competitive, DOE + tokens")
        self.assertIsNotNone(s.raw)
        self.assertIsNone(s.min)  # FR-11: not guessed

    def test_description_is_unmodified(self):
        text = "Hiring!  Backend engineer.\n\n  Remote worldwide.  "
        e = extract(text)
        self.assertEqual(e.description, text)  # FR-12

    def test_contact_extracted(self):
        e = extract("Hiring backend dev. Email jobs@acme.io to apply")
        self.assertEqual(e.contact, "jobs@acme.io")


class FilterTest(unittest.TestCase):
    def _posting(self, text, **kw):
        e = extract(text)
        return Posting(
            title=e.title, description=e.description, source="s", source_tier="A",
            source_url="u", posted_at=utcnow(), collected_at=utcnow(),
            is_remote=kw.get("is_remote", e.is_remote),
            hiring_countries=kw.get("hiring_countries", e.hiring_countries),
            is_worldwide=kw.get("is_worldwide", e.is_worldwide),
        )

    def test_keyword_variants(self):
        self.assertEqual(match_keywords("A full-stack role", ["fullstack"]), ["fullstack"])
        self.assertEqual(match_keywords("full stack dev", ["fullstack"]), ["fullstack"])
        self.assertEqual(match_keywords("FullStack", ["fullstack"]), ["fullstack"])
        self.assertEqual(match_keywords("nothing here", ["fullstack"]), [])

    def test_three_stage_order(self):
        s = UserSettings("u", ["backend"], ["RS"], remote_only=True)
        # keyword miss
        self.assertEqual(evaluate(self._posting("Frontend role remote worldwide"), s).decision, REJECT)
        # remote reject
        p = self._posting("Backend engineer, on-site in Berlin", is_remote="onsite")
        self.assertEqual(evaluate(p, s).decision, REJECT)
        # worldwide passes
        p = self._posting("Backend engineer, remote worldwide", is_worldwide=True)
        self.assertEqual(evaluate(p, s).decision, PASS)
        # undetermined → review
        p = self._posting("Backend engineer. Apply within.", is_remote="unknown",
                          hiring_countries=[], is_worldwide=False)
        self.assertEqual(evaluate(p, s).decision, REVIEW)

    def test_remote_only_false_keeps_onsite(self):
        s = UserSettings("u", ["backend"], [], remote_only=False)
        p = self._posting("Backend engineer, on-site in Berlin", is_remote="onsite",
                          hiring_countries=["DE"])
        self.assertEqual(evaluate(p, s).decision, PASS)


class DedupTest(unittest.TestCase):
    def test_near_match_not_exact(self):
        a = "We are hiring a senior backend engineer for our remote team"
        b = "We are hiring a senior backend engineer for our remote team!!!"
        self.assertTrue(dedup.is_duplicate(a, b))

    def test_apply_url_decisive(self):
        self.assertTrue(dedup.is_duplicate(
            "totally different text one", "completely other words two",
            "https://acme.io/apply", "https://acme.io/apply",
        ))

    def test_distinct_jobs_not_merged(self):
        a = "Hiring a frontend designer in Berlin for our studio"
        b = "Seeking a devops engineer to manage kubernetes clusters remotely"
        self.assertFalse(dedup.is_duplicate(a, b))


class DigestTest(unittest.TestCase):
    def test_never_empty(self):
        self.assertEqual(digest.build_digest({"name": "x"}, []), [])  # NFR-12

    def test_batched_single_message(self):
        postings = [{"title": f"Job {i}", "source": "s", "source_tier": "A",
                     "origins": ["s"], "source_url": "https://x/y", "is_remote": "remote"}
                    for i in range(5)]
        msgs = digest.build_digest({"name": "x"}, postings)
        self.assertEqual(len(msgs), 1)  # FR-31: one digest, not five messages
        self.assertIn("5 new", msgs[0])


class DeliveryRetryTest(unittest.TestCase):
    def test_delivery_retried_and_posting_not_lost(self):
        """NFR-5: a Telegram outage never loses a posting."""
        svc = make_service()
        try:
            svc.store.upsert_source("src", "memory", "A")
            uid = svc.store.create_user("m", keywords=["backend"], eligible_countries=[])
            svc.store.update_user(uid, telegram_chat_id="42")
            from tests.helpers import MEMORY_ITEMS, raw

            MEMORY_ITEMS.clear()
            MEMORY_ITEMS["src"] = [raw("src", "1", "Hiring backend engineer remote worldwide")]
            svc.scheduler.run_once("manual")

            transport = FakeTransport(fail_times=1)  # first send fails
            d = DeliveryService(svc.store, transport)
            d.send_run_digests(None)
            r1 = d.deliver_pending()
            self.assertEqual(r1["failed"], 1)
            # Posting still queued (not marked delivered) — survives the outage.
            self.assertEqual(len(svc.store.pending_delivery(uid)), 1)

            r2 = d.deliver_pending()  # retry succeeds
            self.assertEqual(r2["sent"], 1)
            self.assertEqual(len(svc.store.pending_delivery(uid)), 0)
        finally:
            svc.close()


class MutingTest(unittest.TestCase):
    def test_muted_user_accumulates_but_not_delivered(self):
        """FR-33: mute stops delivery but collection continues."""
        from datetime import timedelta
        svc = make_service()
        try:
            svc.store.upsert_source("src", "memory", "A")
            uid = svc.store.create_user("m", keywords=["backend"])
            svc.store.update_user(uid, telegram_chat_id="42",
                                  muted_until=utcnow() + timedelta(hours=5))
            from tests.helpers import MEMORY_ITEMS, raw
            MEMORY_ITEMS.clear()
            MEMORY_ITEMS["src"] = [raw("src", "1", "Hiring backend engineer remote worldwide")]
            svc.scheduler.run_once("manual")

            transport = FakeTransport()
            d = DeliveryService(svc.store, transport)
            d.send_run_digests(None)
            d.deliver_pending()
            self.assertEqual(len(transport.sent), 0, "muted user gets no push")
            # But the posting is still collected and available.
            self.assertEqual(len(svc.store.pending_delivery(uid)), 1)
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
