"""End-to-end pipeline behaviour for the Telegram-only build."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta

from jobradar.collectors.base import CollectorError
from jobradar.state import State, utcnow
from tests.helpers import MEMORY_ITEMS, FakeTransport, make_pipeline, mem_source, raw


class PipelineTest(unittest.TestCase):
    def setUp(self):
        MEMORY_ITEMS.clear()

    # -- collect → filter → send -------------------------------------------
    def test_scrapes_filters_and_sends(self):
        t = FakeTransport()
        pipe, state, cfg = make_pipeline(
            [mem_source("a"), mem_source("b")], keywords=["backend"], transport=t
        )
        MEMORY_ITEMS["a"] = [raw("a", "1", "We're hiring a backend engineer, fully remote")]
        MEMORY_ITEMS["b"] = [raw("b", "1", "Frontend designer wanted, remote")]  # filtered out
        s = pipe.run()
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(len(t.sent), 1)  # one batched digest
        self.assertIn("backend", t.sent[0]["text"].lower())

    def test_remote_only_rejects_onsite(self):
        t = FakeTransport()
        pipe, *_ = make_pipeline([mem_source("a")], keywords=["backend"],
                                 remote_only=True, transport=t)
        MEMORY_ITEMS["a"] = [raw("a", "1", "Hiring backend engineer, on-site in Berlin only")]
        s = pipe.run()
        self.assertEqual(len(s.sent), 0)
        self.assertEqual(len(t.sent), 0)

    def test_empty_run_sends_nothing(self):
        t = FakeTransport()
        pipe, *_ = make_pipeline([mem_source("a")], keywords=["backend"], transport=t)
        MEMORY_ITEMS["a"] = [raw("a", "1", "Just chatting about frameworks")]
        pipe.run()
        self.assertEqual(len(t.sent), 0)

    # -- dedup across sources ----------------------------------------------
    def test_same_job_two_sources_sent_once_both_origins(self):
        t = FakeTransport()
        pipe, *_ = make_pipeline([mem_source("a"), mem_source("b")],
                                 keywords=["backend"], transport=t)
        text = ("We are hiring a senior backend engineer for our platform. "
                "Fully remote. Apply at https://acme.io/apply/backend")
        MEMORY_ITEMS["a"] = [raw("a", "1", text)]
        MEMORY_ITEMS["b"] = [raw("b", "9", text)]
        s = pipe.run()
        self.assertEqual(len(s.sent), 1)
        self.assertCountEqual(s.sent[0].origins, ["a", "b"])
        self.assertEqual(s.duplicates_merged, 1)

    # -- already-sent memory across runs -----------------------------------
    def test_not_resent_on_next_run(self):
        t = FakeTransport()
        tmp = tempfile.mkdtemp()
        pipe, state, cfg = make_pipeline([mem_source("a")], keywords=["backend"],
                                         transport=t, tmpdir=tmp)
        # Undated item (scraper-style): the source re-emits it every run, so the
        # fingerprint dedup — not the watermark — must stop the resend.
        MEMORY_ITEMS["a"] = [raw("a", "1", "Hiring backend engineer, fully remote", dated=False)]
        pipe.run()
        self.assertEqual(len(t.sent), 1)
        s2 = pipe.run()
        self.assertEqual(len(s2.sent), 0)
        self.assertEqual(len(t.sent), 1)
        self.assertGreaterEqual(s2.already_sent_skipped, 1)

    def test_state_persists_to_disk(self):
        t = FakeTransport()
        tmp = tempfile.mkdtemp()
        pipe, state, cfg = make_pipeline([mem_source("a")], keywords=["backend"],
                                         transport=t, tmpdir=tmp)
        MEMORY_ITEMS["a"] = [raw("a", "1", "Hiring backend engineer, fully remote")]
        pipe.run()
        # A fresh State reading the same file remembers what was sent.
        reloaded = State(cfg.state_path)
        self.assertEqual(len(reloaded.sent), 1)

    def test_not_marked_sent_without_telegram(self):
        # No transport → nothing sent, and NOT recorded, so a later run with
        # Telegram configured will send it.
        pipe, state, cfg = make_pipeline([mem_source("a")], keywords=["backend"], transport=None)
        MEMORY_ITEMS["a"] = [raw("a", "1", "Hiring backend engineer, fully remote")]
        s = pipe.run()
        self.assertEqual(len(s.sent), 1)   # detected
        self.assertEqual(len(state.sent), 0)  # but not marked sent

    # -- failure isolation + Tier B auto-disable ---------------------------
    def test_one_source_failure_does_not_stop_others(self):
        t = FakeTransport()
        pipe, *_ = make_pipeline([mem_source("good"), mem_source("bad", tier="A")],
                                 keywords=["backend"], transport=t)

        def boom():
            raise CollectorError("network down")

        MEMORY_ITEMS["good"] = [raw("good", "1", "Hiring backend engineer, fully remote")]
        MEMORY_ITEMS["bad"] = boom
        s = pipe.run()
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(s.per_source["bad"]["status"], "error")

    def test_tier_b_autodisables_after_three_failures(self):
        t = FakeTransport()
        tmp = tempfile.mkdtemp()
        pipe, state, cfg = make_pipeline([mem_source("scraper", tier="B")],
                                         keywords=["backend"], transport=t, tmpdir=tmp)

        def boom():
            raise CollectorError("markup changed")

        MEMORY_ITEMS["scraper"] = boom
        for _ in range(3):
            s = pipe.run()
        self.assertTrue(state.is_disabled("scraper"))
        self.assertTrue(any("auto-disabled" in a for a in s.alerts))
        # Owner was notified over Telegram.
        self.assertTrue(any("auto-disabled" in m["text"] for m in t.sent))

    # -- delivery retry on outage ------------------------------------------
    def test_outage_retries_and_does_not_lose_posting(self):
        t = FakeTransport(fail_times=1)  # first send fails
        pipe, state, cfg = make_pipeline([mem_source("a")], keywords=["backend"], transport=t)
        MEMORY_ITEMS["a"] = [raw("a", "1", "Hiring backend engineer, fully remote")]
        s1 = pipe.run()
        self.assertEqual(len(t.sent), 0)          # outage
        self.assertEqual(len(state.sent), 0)      # not marked → will retry
        self.assertTrue(any("telegram send failed" in a for a in s1.alerts))
        # Next run: send succeeds, posting delivered.
        s2 = pipe.run()
        self.assertEqual(len(t.sent), 1)
        self.assertEqual(len(state.sent), 1)


if __name__ == "__main__":
    unittest.main()
