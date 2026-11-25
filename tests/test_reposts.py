"""Cross-run detection of reworded reposts, and the role guard that keeps
different jobs from one company apart."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar import dedup
from jobradar.collectors.registry import REGISTRY
from jobradar.config import Config
from jobradar.models import RawItem
from jobradar.pipeline import Pipeline
from jobradar.state import State, iso, utcnow

HN = """Acme Robotics | Senior Backend Engineer | Remote (US/EU) | Full-time | $160k-$200k + equity
We build fleet-management software for warehouse robots. Our backend is Python and Go on AWS, with Postgres, Kafka and Kubernetes. You'll own services that coordinate thousands of robots in real time, design APIs used by our customers, and help us scale from 40 to 200 sites this year.
Requirements: 5+ years backend experience, strong Python or Go, distributed systems, comfortable with on-call.
Nice to have: robotics, ROS, gRPC.
We offer: fully remote, 4-week minimum PTO, home office budget, top-tier health insurance.
Apply: https://acme-robotics.example/careers/backend or email jobs@acme-robotics.example"""

NEXT_MONTH = HN.replace("$160k-$200k", "$170k-$210k").replace("40 to 200 sites", "60 to 250 sites")
REWORDED = (HN.replace("You'll own services that coordinate", "You will be responsible for services coordinating")
              .replace("help us scale", "help scale")
              .replace("comfortable with on-call", "happy to join an on-call rotation"))
JUNIOR = (HN.replace("Senior Backend Engineer", "Backend Engineer (Junior)")
            .replace("$160k-$200k + equity", "$100k-$130k")
            .replace("5+ years backend experience", "1-2 years backend experience")
            .replace("careers/backend", "careers/backend-junior"))
FRONTEND = (HN.replace("Senior Backend Engineer", "Senior Frontend Engineer")
              .replace("Our backend is Python and Go on AWS, with Postgres, Kafka and Kubernetes.",
                       "Our dashboard is React and TypeScript.")
              .replace("5+ years backend experience, strong Python or Go, distributed systems, "
                       "comfortable with on-call.", "5+ years frontend experience, strong React.")
              .replace("careers/backend", "careers/frontend"))
OTHER_COMPANY = """Fintechly | Senior Backend Engineer | Remote (EU) | €90k-€120k
We're building payments infrastructure for European SMBs. Stack: Python, Go, Postgres, Kafka, Kubernetes on AWS. You'll design APIs for our merchants and own services in the money-movement path.
Requirements: 5+ years backend experience, strong Python or Go, distributed systems.
Fully remote, 30 days PTO. Apply: jobs@fintechly.example"""


def title(text):
    return text.splitlines()[0]


class RoleGuardTests(unittest.TestCase):
    def test_role_signature(self):
        cases = {
            "Acme | Senior Backend Engineer | Remote": ({"senior"}, {"backend"}),
            "Sr. Front-end Developer": ({"senior"}, {"frontend"}),
            "高级后端开发工程师": ({"senior"}, {"backend"}),
            "Engineering Manager, Payments": ({"manager"}, set()),
            "SRE / DevOps (Senior)": ({"senior"}, {"devops"}),
            "Acme Robotics is hiring!": (set(), set()),
        }
        for t, (sen, fun) in cases.items():
            with self.subTest(t):
                self.assertEqual(dedup.role_signature(t), (frozenset(sen), frozenset(fun)))

    def test_conflict_needs_both_sides_and_no_overlap(self):
        sig = dedup.role_signature
        self.assertTrue(dedup.roles_conflict(sig("Senior Backend"), sig("Junior Backend")))
        self.assertTrue(dedup.roles_conflict(sig("Backend Engineer"), sig("Frontend Engineer")))
        self.assertFalse(dedup.roles_conflict(sig("Backend Engineer"), sig("Senior Backend Engineer")))
        self.assertFalse(dedup.roles_conflict(sig("Backend, Frontend"), sig("Frontend Engineer")))
        self.assertFalse(dedup.roles_conflict(sig("We're hiring!"), sig("Junior Backend")))

    def test_within_run_merge_keeps_different_roles_apart(self):
        # Shares ~75% of its text with HN, so this used to be merged away.
        self.assertFalse(dedup.is_duplicate(HN, JUNIOR, title_a=title(HN), title_b=title(JUNIOR)))
        self.assertTrue(dedup.is_duplicate(HN, REWORDED, title_a=title(HN), title_b=title(REWORDED)))

    def test_shared_careers_page_url_doesnt_merge_different_roles(self):
        url = "https://acme.example/careers"
        self.assertFalse(dedup.is_duplicate("a", "b", url, url, title_a="Backend Engineer",
                                            title_b="Frontend Engineer"))
        self.assertTrue(dedup.is_duplicate("a", "b", url, url))


class MinHashTests(unittest.TestCase):
    def test_signature_is_stable(self):
        # Signatures are persisted in the state file: the hashing must never change.
        t = ("Acme Robotics | Senior Backend Engineer | Remote (US/EU) | Full-time. We build "
             "fleet-management software for warehouse robots in Python and Go.")
        self.assertTrue(dedup.minhash(t).startswith("a5LoiizurE4TsA1lostb0nKy"))

    def test_estimate_tracks_jaccard(self):
        for other in (NEXT_MONTH, REWORDED, JUNIOR, FRONTEND, OTHER_COMPANY):
            with self.subTest(other=other[:30]):
                est = dedup.estimate_similarity(dedup.minhash(HN), dedup.minhash(other))
                self.assertAlmostEqual(est, dedup.jaccard(HN, other), delta=0.15)

    def test_short_posts_get_no_signature(self):
        self.assertIsNone(dedup.minhash("Hiring React dev, DM me"))

    def test_index(self):
        idx = dedup.RepostIndex()
        idx.add("hn", dedup.minhash(HN), dedup.encode_roles(dedup.role_signature(title(HN))))
        idx.add("corrupt", "not-base64!", "|")          # ignored, doesn't crash
        find = lambda text: idx.find(dedup.minhash(text), dedup.role_signature(title(text)))
        self.assertEqual(find(NEXT_MONTH), "hn")
        self.assertEqual(find(REWORDED), "hn")
        self.assertIsNone(find(JUNIOR))                 # similar text, different role
        self.assertIsNone(find(OTHER_COMPANY))
        self.assertEqual(len(idx), 1)


# ------------------------------------------------------------ via the pipeline
class _Collector:
    type = "rss"
    items: list[str] = []

    def __init__(self, *, name, tier, config, http):
        self.name = name

    def fetch(self, ctx):
        for i, text in enumerate(self.items):
            yield RawItem(source=self.name, external_id=str(i), raw_text=text,
                          url=f"https://example.com/{i}", posted_at=utcnow() - timedelta(hours=1))


class _Transport:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append(text)


class PipelineRepostTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "jobradar.db"
        patcher = mock.patch.dict(REGISTRY, {"rss": _Collector})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = Config(keywords=[], remote_only=False, telegram_chat_id="1",
                          message_interval_seconds=0)

    def run_pipeline(self, items, *, window=60):
        _Collector.items = items
        # A fresh State each time: proves the memory survives save/load.
        state = State(self.path, repost_window_days=window)
        cfg = Config(**{**self.cfg.__dict__, "repost_window_days": window})
        return Pipeline(cfg, state, [{"name": "hn", "type": "rss", "tier": "A"}],
                        transport=_Transport()).run()

    def test_reworded_repost_in_a_later_run_is_not_sent_again(self):
        self.assertEqual(self.run_pipeline([HN]).delivered, 1)
        s = self.run_pipeline([NEXT_MONTH])
        self.assertEqual((s.delivered, s.reposts_skipped), (0, 1))

    def test_drift_over_several_reposts_is_followed(self):
        self.run_pipeline([HN])
        self.run_pipeline([NEXT_MONTH])
        drifted = NEXT_MONTH.replace("You'll own services that coordinate",
                                     "You will own services coordinating").replace(
            "4-week minimum PTO", "five weeks of PTO")
        self.assertEqual(self.run_pipeline([drifted]).reposts_skipped, 1)

    def test_different_jobs_are_still_delivered(self):
        self.run_pipeline([HN])
        s = self.run_pipeline([JUNIOR, FRONTEND, OTHER_COMPANY])
        self.assertEqual((s.delivered, s.reposts_skipped), (3, 0))

    def test_memory_expires_after_the_window(self):
        self.run_pipeline([HN])
        state = State(self.path)
        with state._tx() as db:  # age every remembered signature past the window
            db.execute("UPDATE signatures SET seen_at = ?", (iso(utcnow() - timedelta(days=61)),))
        self.assertEqual(self.run_pipeline([NEXT_MONTH]).delivered, 1)

    def test_window_zero_disables_it(self):
        self.run_pipeline([HN], window=0)
        self.assertEqual(self.run_pipeline([NEXT_MONTH], window=0).delivered, 1)
        self.assertEqual(State(self.path).signature_count(), 0)


if __name__ == "__main__":
    unittest.main()
