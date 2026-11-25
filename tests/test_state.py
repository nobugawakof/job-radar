"""The SQLite state database: persistence, JSON import, safety and limits."""

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from jobradar.config import Config
from jobradar.service import Service
from jobradar.state import SCHEMA_VERSION, State, iso, utcnow


class StateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "jobradar.db"


class PersistenceTests(StateTestCase):
    def test_everything_survives_a_restart(self):
        s = State(self.db)
        s.mark_sent("fp1")
        s.record_success("hn", utcnow())
        s.record_failure("scraper")
        s.disable_source("scraper")
        s.mark_run(utcnow())
        s.record_job({"fingerprint": "fp1", "title": "Go dev", "url": "https://x", "description": "full text"})
        s.queue_put("fp2", {"title": "Queued", "posted_at": iso(utcnow()), "telegram": "@hr_anna"})
        s.mark_contact("tg:@hr_anna", "auto", utcnow())
        s.set_paused("telegram", iso(utcnow() + timedelta(hours=1)))
        s.close()

        r = State(self.db)
        self.assertTrue(r.already_sent("fp1"))
        self.assertFalse(r.already_sent("nope"))
        self.assertIsNotNone(r.source_last_success("hn"))
        self.assertEqual(r.source("scraper")["failures"], 1)
        self.assertTrue(r.is_disabled("scraper"))
        self.assertIsNotNone(r.last_run)
        self.assertEqual(r.recent_jobs(with_text=True)[0]["description"], "full text")
        self.assertEqual(r.queue_items()[0][1]["telegram"], "@hr_anna")
        self.assertIsNotNone(r.contact_last("tg:@hr_anna", "auto"))
        self.assertIn("telegram", r.paused_channels())

    def test_finishing_an_application_also_dequeues_it(self):
        s = State(self.db)
        s.queue_put("fp", {"title": "x"})
        s.finish_application("fp", {"at": iso(utcnow()), "mode": "auto", "status": "sent", "score": 88})
        self.assertFalse(s.queue_has("fp"))
        self.assertEqual(s.get_application("fp")["score"], 88)
        self.assertEqual(s.count_applications(mode="auto", statuses=("sent",),
                                              since=utcnow() - timedelta(hours=1)), 1)
        self.assertEqual(s.application_counts(), {"sent": 1})

    def test_failed_transaction_leaves_nothing_behind(self):
        s = State(self.db)
        with self.assertRaises(RuntimeError):
            with s._tx() as db:
                db.execute("INSERT INTO sent VALUES ('half', ?)", (iso(utcnow()),))
                raise RuntimeError("crash mid-write")
        self.assertFalse(s.already_sent("half"))

    def test_size_limits(self):
        s = State(self.db, max_sent=3, repost_window_days=30)
        for i in range(6):
            when = utcnow() - timedelta(days=6 - i)
            s.mark_sent(f"fp{i}", when)
            s.record_job({"title": f"job {i}", "at": iso(when)})
        s.remember_signature("old", "sig", "|", utcnow() - timedelta(days=40))
        s.remember_signature("new", "sig", "|", utcnow())
        s.save()
        self.assertEqual(s.sent_count(), 3)
        self.assertTrue(s.already_sent("fp5") and not s.already_sent("fp0"))  # newest kept
        self.assertEqual([j["title"] for j in s.recent_jobs()], ["job 5", "job 4", "job 3"])
        self.assertEqual(s.signature_count(), 1)  # older than the repost window

    def test_many_threads_at_once(self):
        s = State(self.db)
        errors = []

        def work(n):
            try:
                for i in range(100):
                    s.mark_sent(f"t{n}-{i}")
                    s.already_sent(f"t{n}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((errors, s.sent_count()), ([], 800))

    def test_two_programs_share_the_file(self):
        dashboard, cli = State(self.db), State(self.db)
        dashboard.mark_sent("from-dashboard")
        self.assertTrue(cli.already_sent("from-dashboard"))
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class SafetyTests(StateTestCase):
    def test_corrupt_file_is_set_aside(self):
        self.db.write_bytes(b"this is not a database" * 100)
        s = State(self.db)
        s.mark_sent("fresh")
        self.assertTrue(s.already_sent("fresh"))
        self.assertTrue(list(self.dir.glob("jobradar.db.corrupt-*")))

    def test_newer_schema_is_refused(self):
        db = sqlite3.connect(self.db)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        db.close()
        with self.assertRaises(sqlite3.DatabaseError):
            State(self.db)
        self.assertTrue(self.db.exists())  # left untouched


class JsonImportTests(StateTestCase):
    LEGACY = {
        "sent": {"fpA": "2026-11-20T10:00:00+00:00", "fpB": "2026-11-21T10:00:00+00:00"},
        "sources": {"hackernews": {"last_success": "2026-11-21T09:00:00+00:00", "failures": 0, "disabled": False},
                    "broken": {"last_success": None, "failures": 3, "disabled": True}},
        "last_run": "2026-11-21T10:00:00+00:00",
        "signatures": {"fpA": ["2026-11-20T10:00:00+00:00", "c2ln", "senior|backend"]},
        "recent": [{"at": "2026-11-21T10:00:00+00:00", "title": "Newest", "url": "u2", "source": "hn"},
                   {"at": "2026-11-20T10:00:00+00:00", "title": "Older", "url": "u1", "source": "hn"}],
        "applications": {"fpA": {"at": "2026-11-20T11:00:00+00:00", "mode": "auto", "status": "sent",
                                 "channel": "telegram", "contact": "@hr_anna", "title": "Go dev",
                                 "source_url": "u1", "score": 81, "reason": "good", "error": ""}},
        "apply_queue": {"fpC": {"title": "Waiting", "posted_at": "2026-11-21T08:00:00+00:00",
                                "queued_at": "2026-11-21T10:00:00+00:00", "attempts": 1, "email": "jobs@x.io"}},
        "contacts": {"tg:@hr_anna": {"auto": "2026-11-20T11:00:00+00:00"}},
        "apply_paused": {"telegram": "2030-01-01T00:00:00+00:00"},
    }

    def write_legacy(self, name="jobradar-state.json"):
        (self.dir / name).write_text(json.dumps(self.LEGACY), "utf-8")
        return self.dir / name

    def service(self, **cfg):
        return Service(Config(**cfg), config_path=str(self.dir / "config.toml"))

    def test_old_state_file_is_imported_once(self):
        self.write_legacy()
        st = self.service().state
        self.assertEqual(Path(st.path).name, "jobradar.db")
        self.assertTrue(st.already_sent("fpA") and st.already_sent("fpB"))
        self.assertTrue(st.is_disabled("broken"))
        self.assertEqual(st.last_run, "2026-11-21T10:00:00+00:00")
        self.assertEqual(st.signature_count(), 1)
        self.assertEqual([j["title"] for j in st.recent_jobs()], ["Newest", "Older"])
        self.assertEqual(st.get_application("fpA")["score"], 81)
        self.assertEqual(st.queue_items()[0][1]["attempts"], 1)
        self.assertIsNotNone(st.contact_last("tg:@hr_anna", "auto"))
        self.assertIn("telegram", st.paused_channels())
        # The JSON is kept as a backup, and isn't imported a second time.
        self.assertFalse((self.dir / "jobradar-state.json").exists())
        self.assertTrue((self.dir / "jobradar-state.json.migrated").exists())
        st.close()
        self.write_legacy()
        again = self.service().state
        self.assertEqual(len(again.recent_jobs()), 2)

    def test_custom_json_state_path_becomes_a_db_beside_it(self):
        self.write_legacy("my-state.json")
        st = self.service(state_path="my-state.json").state
        self.assertEqual(Path(st.path).name, "my-state.db")
        self.assertTrue(st.already_sent("fpA"))

    def test_unreadable_json_is_skipped(self):
        (self.dir / "jobradar-state.json").write_text("{broken", "utf-8")
        st = self.service().state
        self.assertEqual(st.sent_count(), 0)


if __name__ == "__main__":
    unittest.main()
