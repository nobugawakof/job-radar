"""Acceptance criteria AC-1 .. AC-9 (Section 8).

AC-10 ("the owner judges the postings worth reading") is explicitly the one
criterion that cannot be automated, so it is not tested here.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from jobradar.collectors.base import CollectorError
from jobradar.db import iso, parse_iso, utcnow
from jobradar.delivery.telegram import DeliveryService, TelegramBot
from jobradar.models import STATUS_NEW, STATUS_PENDING_REVIEW
from tests.helpers import MEMORY_ITEMS, FakeTransport, make_service, raw


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        MEMORY_ITEMS.clear()
        self.svc = make_service()
        self.store = self.svc.store

    def tearDown(self) -> None:
        self.svc.close()

    def _add_source(self, name: str, tier: str = "A") -> None:
        self.store.upsert_source(name, "memory", tier)

    def _member(self, name: str, *, keywords, countries=None, remote_only=True, chat=None):
        uid = self.store.create_user(
            name, keywords=keywords, eligible_countries=countries or [], remote_only=remote_only
        )
        if chat:
            self.store.update_user(uid, telegram_chat_id=chat)
        return uid

    # -- AC-1 ---------------------------------------------------------------
    def test_ac1_two_tier_a_sources_repeating_schedule(self) -> None:
        self._add_source("src-a")
        self._add_source("src-b")
        self._member("m", keywords=["backend"])

        MEMORY_ITEMS["src-a"] = [raw("src-a", "1", "Hiring backend engineer, remote worldwide")]
        MEMORY_ITEMS["src-b"] = [raw("src-b", "1", "Backend developer wanted, remote anywhere")]
        s1 = self.svc.scheduler.run_once("scheduled")
        self.assertEqual(s1.items_fetched, 2)
        self.assertEqual(s1.postings_detected, 2)
        self.assertIsNotNone(self.store.get_source("src-a")["last_success_at"])

        # A second scheduled run with newer items collects again (repeating).
        later = _now() + timedelta(hours=1)
        MEMORY_ITEMS["src-a"] = [raw("src-a", "2", "Hiring backend dev remote worldwide", posted_at=later)]
        MEMORY_ITEMS["src-b"] = []
        s2 = self.svc.scheduler.run_once("scheduled")
        self.assertEqual(s2.postings_detected, 1)

    # -- AC-2 ---------------------------------------------------------------
    def test_ac2_startup_catchup_recovers_missed_window(self) -> None:
        self._add_source("src")
        self._member("m", keywords=["backend"])
        # Pretend the last run was a day ago (laptop was closed overnight).
        self.store.db.set_meta("scheduler.last_run_at", iso(_now() - timedelta(hours=24)))
        self.assertTrue(self.svc.scheduler.is_catchup_due())

        MEMORY_ITEMS["src"] = [raw("src", "1", "Hiring backend engineer remote worldwide")]
        summary = self.svc.scheduler.startup()
        self.assertIsNotNone(summary)
        self.assertEqual(summary.trigger, "catchup")
        self.assertEqual(summary.postings_detected, 1)
        # After a catch-up, another is not immediately due.
        self.assertFalse(self.svc.scheduler.is_catchup_due())

    # -- AC-3 ---------------------------------------------------------------
    def test_ac3_worldwide_passes_when_no_country_matches(self) -> None:
        self._add_source("src")
        # Eligible only in Serbia; the post names no country, only "worldwide".
        uid = self._member("m", keywords=["backend"], countries=["RS"])
        MEMORY_ITEMS["src"] = [raw("src", "1",
            "We're hiring a backend engineer. Fully remote, worldwide. Apply: jobs@acme.io")]
        self.svc.scheduler.run_once("manual")

        pending = self.store.pending_delivery(uid)
        self.assertEqual(len(pending), 1, "worldwide posting must pass for an RS-only user (FR-19)")
        self.assertTrue(self.store.get_posting(pending[0]["id"])["is_worldwide"])

    # -- AC-4 ---------------------------------------------------------------
    def test_ac4_no_location_goes_to_review_and_resolves_from_telegram(self) -> None:
        self._add_source("src")
        uid = self._member("m", keywords=["backend"], countries=["RS"], chat="555")
        MEMORY_ITEMS["src"] = [raw("src", "1",
            "Hiring a backend developer. Send your CV to jobs@acme.io")]  # no location
        self.svc.scheduler.run_once("manual")

        queue = self.store.review_queue(uid)
        self.assertEqual(len(queue), 1, "undetermined geography must go to review (FR-20)")
        pid = queue[0]["id"]

        # Resolve from Telegram with a single tap (FR-23).
        transport = FakeTransport()
        bot = TelegramBot(self.store, transport)
        bot.process_update({"callback_query": {
            "id": "cb1", "data": f"rev:{pid}:yes",
            "message": {"chat": {"id": "555"}},
        }})
        row = self.store.db.query_one(
            "SELECT status FROM user_postings WHERE user_id=? AND posting_id=?", (uid, pid)
        )
        self.assertEqual(row["status"], STATUS_NEW)  # relevant → delivered
        self.assertEqual(len(self.store.review_queue(uid)), 0)

    # -- AC-5 ---------------------------------------------------------------
    def test_ac5_same_job_two_sources_delivered_once_both_origins(self) -> None:
        self._add_source("src-a")
        self._add_source("src-b")
        uid = self._member("m", keywords=["backend"])
        text = ("We are hiring a senior backend engineer to work on our platform. "
                "Remote, worldwide. Apply at https://acme.io/apply/backend")
        MEMORY_ITEMS["src-a"] = [raw("src-a", "1", text)]
        MEMORY_ITEMS["src-b"] = [raw("src-b", "9", text)]
        self.svc.scheduler.run_once("manual")

        pending = self.store.pending_delivery(uid)
        self.assertEqual(len(pending), 1, "duplicate across sources delivered once (FR-27)")
        self.assertCountEqual(pending[0]["origins"], ["src-a", "src-b"])  # FR-29

    # -- AC-6 ---------------------------------------------------------------
    def test_ac6_two_members_different_keywords_different_digests(self) -> None:
        self._add_source("src")
        a = self._member("alice", keywords=["backend"])
        b = self._member("bob", keywords=["frontend"])
        MEMORY_ITEMS["src"] = [
            raw("src", "1", "Hiring backend engineer, remote worldwide"),
            raw("src", "2", "Hiring frontend developer, remote worldwide"),
        ]
        self.svc.scheduler.run_once("manual")

        a_titles = [p["title"] for p in self.store.pending_delivery(a)]
        b_titles = [p["title"] for p in self.store.pending_delivery(b)]
        self.assertTrue(any("backend" in t.lower() for t in a_titles))
        self.assertFalse(any("frontend" in t.lower() for t in a_titles))
        self.assertTrue(any("frontend" in t.lower() for t in b_titles))
        self.assertFalse(any("backend" in t.lower() for t in b_titles))

    # -- AC-7 ---------------------------------------------------------------
    def test_ac7_disabling_tier_b_leaves_tier_a_functional(self) -> None:
        self._add_source("tier-a", tier="A")
        self._add_source("tier-b", tier="B")
        uid = self._member("m", keywords=["backend"])
        self.store.set_source_enabled("tier-b", False)

        MEMORY_ITEMS["tier-a"] = [raw("tier-a", "1", "Hiring backend engineer remote worldwide")]
        MEMORY_ITEMS["tier-b"] = [raw("tier-b", "1", "should not be fetched")]
        summary = self.svc.scheduler.run_once("manual")

        self.assertNotIn("tier-b", summary.per_source)
        self.assertEqual(len(self.store.pending_delivery(uid)), 1)

    # -- AC-8 ---------------------------------------------------------------
    def test_ac8_broken_tier_b_autodisables_after_three_failures(self) -> None:
        owner = self.store.create_user("owner", is_owner=True, keywords=["backend"])
        self.store.update_user(owner, telegram_chat_id="1")
        self._add_source("tier-a", tier="A")
        self._add_source("broken", tier="B")
        self._member("m", keywords=["backend"])

        def boom():
            raise CollectorError("markup changed")

        MEMORY_ITEMS["broken"] = boom
        MEMORY_ITEMS["tier-a"] = [raw("tier-a", "1", "Hiring backend engineer remote worldwide")]

        last = None
        for _ in range(3):
            last = self.svc.scheduler.run_once("scheduled")

        src = self.store.get_source("broken")
        self.assertFalse(src["enabled"], "Tier B must auto-disable after 3 failures (SR-4)")
        self.assertGreaterEqual(src["consecutive_failures"], 3)
        self.assertTrue(any("auto-disabled" in a for a in last.alerts))
        # Owner was notified via the durable delivery queue.
        alerts = self.store.pending_deliveries()
        self.assertTrue(any(d["payload"].get("type") == "alert" for d in alerts))
        # Tier A unaffected across all three runs.
        self.assertEqual(self.store.get_source("tier-a")["consecutive_failures"], 0)

    # -- AC-9 ---------------------------------------------------------------
    def test_ac9_crash_resume_consistent_and_correct(self) -> None:
        self._add_source("src")
        uid = self._member("m", keywords=["backend"])

        # Simulate a crash *after* raw items were durably stored but *before*
        # they were processed (FR-6 stores raw first). Insert raw directly and
        # leave them unprocessed, as a killed run would.
        self.store.store_raw_item(raw("src", "1", "Hiring backend engineer remote worldwide"))
        self.store.store_raw_item(raw("src", "2", "Backend developer wanted, remote anywhere"))
        unprocessed = self.store.db.query("SELECT COUNT(*) c FROM raw_items WHERE processed=0")[0]["c"]
        self.assertEqual(unprocessed, 2)

        # The next run must pick up and process the leftover raw items.
        MEMORY_ITEMS["src"] = []
        self.svc.scheduler.run_once("scheduled")
        remaining = self.store.db.query("SELECT COUNT(*) c FROM raw_items WHERE processed=0")[0]["c"]
        self.assertEqual(remaining, 0, "leftover raw items must be processed on resume")
        self.assertEqual(len(self.store.pending_delivery(uid)), 2)

        # Re-collecting the same items is idempotent — no duplicate raw rows.
        self.assertIsNone(self.store.store_raw_item(raw("src", "1", "dup")))

    def test_ac9_poison_item_does_not_wedge_the_batch(self) -> None:
        """NFR-6: a single malformed post must not abort its batch."""
        self._add_source("src")
        uid = self._member("m", keywords=["backend"])
        good = raw("src", "1", "Hiring backend engineer remote worldwide")
        bad = raw("src", "2", "Hiring backend engineer remote worldwide")
        MEMORY_ITEMS["src"] = [good, bad]

        # Force processing of one item to blow up; the other must still land.
        original = self.svc.pipeline._process_one
        state = {"n": 0}

        def flaky(row, now, summary):
            state["n"] += 1
            if state["n"] == 1:
                raise ValueError("boom")
            return original(row, now, summary)

        self.svc.pipeline._process_one = flaky  # type: ignore[assignment]
        self.svc.scheduler.run_once("scheduled")
        # Both raw rows end processed; at least one posting survived.
        remaining = self.store.db.query("SELECT COUNT(*) c FROM raw_items WHERE processed=0")[0]["c"]
        self.assertEqual(remaining, 0)
        self.assertGreaterEqual(len(self.store.pending_delivery(uid)), 1)


if __name__ == "__main__":
    unittest.main()
