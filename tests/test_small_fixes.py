"""B6 contacts masked in logs, B7 age-limit default, B8 one check at a time
across programs, B9 run history kept in the database."""

import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar.apply.senders import ContactError, EmailSender
from jobradar.config import Config
from jobradar.redact import mask_contact
from jobradar.service import AlreadyRunning, Service
from jobradar.state import State, iso, utcnow


class MaskContactTests(unittest.TestCase):  # B6
    def test_mask(self):
        self.assertEqual(mask_contact("@hr_anna"), "@h***na")
        self.assertEqual(mask_contact("jane.doe@acme.io"), "j***@acme.io")

    def test_send_errors_dont_contain_the_contact(self):
        sender = EmailSender(host="h", port=587, user="u", password="p", sender="me@x.io")
        fake = mock.MagicMock()
        fake.send_message.side_effect = smtplib.SMTPRecipientsRefused({"jane.doe@acme.io": (550, b"no")})
        sender._smtp = fake
        with self.assertRaises(ContactError) as cm:
            sender.send("jane.doe@acme.io", message="hi", subject="s")
        self.assertNotIn("jane.doe", str(cm.exception))
        self.assertIn("j***@acme.io", str(cm.exception))


class DefaultsTests(unittest.TestCase):  # B7
    def test_age_limit_matches_docs(self):
        self.assertEqual(Config().max_posting_age_days, 15)


class RunLockTests(unittest.TestCase):  # B8
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "jobradar.db"

    def test_second_program_waits_its_turn(self):
        dashboard, serve = State(self.db), State(self.db)
        self.assertTrue(dashboard.acquire_run_lock("dashboard"))
        self.assertFalse(serve.acquire_run_lock("serve"))
        dashboard.release_run_lock("dashboard")
        self.assertTrue(serve.acquire_run_lock("serve"))

    def test_lock_of_a_dead_process_is_taken_over(self):
        s = State(self.db)
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True).stdout.strip()
        with s._tx() as db:
            import json, socket
            db.execute("INSERT OR REPLACE INTO meta VALUES ('run_lock', ?)", (json.dumps(
                {"owner": "crashed", "pid": int(dead), "host": socket.gethostname(), "at": iso(utcnow())}),))
        self.assertTrue(s.acquire_run_lock("me"))

    def test_stale_lock_is_taken_over(self):
        s = State(self.db)
        self.assertTrue(s.acquire_run_lock("old"))
        self.assertTrue(s.acquire_run_lock("new", ttl=timedelta(seconds=-1)))

    def service(self):
        return Service(Config(), config_path=str(self.dir / "config.toml"))

    def test_service_refuses_to_run_twice(self):
        svc = self.service()
        other = State(svc.state.path)
        self.assertTrue(other.acquire_run_lock("someone-else"))
        with self.assertRaises(AlreadyRunning):
            svc.run("manual")
        self.assertIsNone(svc._safe_run("scheduled"))  # skipped quietly...
        self.assertIsNone(svc.state.last_run)           # ...without pretending it ran

    def test_busy_scheduler_doesnt_spin(self):
        svc = self.service()
        svc.state.mark_run(utcnow() - timedelta(hours=10))  # a check is due
        State(svc.state.path).acquire_run_lock("someone-else")
        sleeps = []

        def fake_sleep(n):
            sleeps.append(n)
            raise KeyboardInterrupt

        with mock.patch("jobradar.service.time.sleep", fake_sleep), self.assertRaises(KeyboardInterrupt):
            svc.run_forever(poll_seconds=42)
        self.assertEqual(sleeps, [42])


class RunHistoryTests(unittest.TestCase):  # B9
    def test_every_check_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            svc = Service(Config(), config_path=str(Path(d) / "config.toml"))
            svc.pipeline.source_defs = []  # no network
            svc.run("manual")
            svc.run("scheduled")
            runs = svc.state.recent_runs()
            self.assertEqual([r["trigger"] for r in runs], ["scheduled", "manual"])
            self.assertEqual(svc.state.last_run_summary()["trigger"], "scheduled")
            self.assertIsNone(svc.state.run_lock_holder())  # released

    def test_older_database_gains_the_history_table(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "jobradar.db"
            raw = sqlite3.connect(db)
            raw.execute("CREATE TABLE sent (fingerprint TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
            raw.execute("INSERT INTO sent VALUES ('old', '2026-01-01T00:00:00+00:00')")
            raw.execute("PRAGMA user_version=1")
            raw.commit()
            raw.close()
            s = State(db)
            self.assertTrue(s.already_sent("old"))
            s.record_run({"trigger": "manual", "at": iso(utcnow()), "sources": []})
            self.assertEqual(len(s.recent_runs()), 1)


if __name__ == "__main__":
    unittest.main()
