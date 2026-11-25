import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobradar.ai import ApplicationDraft
from jobradar.apply.engine import Applier, template_draft
from jobradar.apply.senders import ChannelUnavailable, ContactError
from jobradar.config import Config
from jobradar.models import Posting
from jobradar.state import State

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def posting(n: int, text: str, *, source_type: str = "telegram", age_days: float = 0.0) -> Posting:
    return Posting(
        title=f"Backend Engineer {n}", description=text, source="tg-jobs", source_tier="A",
        source_url=f"https://t.me/jobs/{n}", posted_at=NOW - timedelta(days=age_days),
        collected_at=NOW, content_hash=f"hash{n}", source_type=source_type,
    )


class FakeSender:
    def __init__(self, channel, fail=None):
        self.channel = channel
        self.sent = []
        self.fail = fail or {}   # contact -> exception to raise
        self.closed = False

    def send(self, contact, *, message, subject, attachment):
        if contact in self.fail:
            raise self.fail[contact]
        self.sent.append((contact, message, subject, attachment))

    def close(self):
        self.closed = True


def draft(score=85):
    return ApplicationDraft(fit_score=score, fit_reason="good match", message="Hi, I'd like to apply",
                            email_subject="Application", email_body="Dear team, ...")


class ApplierTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.resume = self.dir / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4")
        self.state = State(self.dir / "state.json")
        self.notes: list[str] = []
        self.sleeps: list[float] = []
        self.tg = FakeSender("telegram")
        self.mail = FakeSender("email")
        self.cfg = Config(apply_mode="auto", apply_daily_cap=10, apply_min_score=70,
                          apply_delay_seconds=[45, 120])
        self.score = 85

    def tearDown(self):
        self.tmp.cleanup()

    def applier(self, **cfg):
        config = replace(self.cfg, **cfg)
        return Applier(config, self.state, senders={"telegram": self.tg, "email": self.mail},
                       drafter=lambda entry: draft(self.score), notify=self.notes.append,
                       resume_pdf=self.resume, ai_available=True, sleep=self.sleeps.append,
                       rand=lambda lo, hi: lo)


class DryRunTests(ApplierTestCase):
    def test_dry_run_previews_and_contacts_nobody(self):
        r = self.applier(apply_mode="dry_run").run([posting(1, "Hiring. Contact @hr_anna")], NOW)
        self.assertEqual((r.previewed, r.sent), (1, 0))
        self.assertEqual(self.tg.sent, [])
        self.assertIn("DRY RUN", self.notes[0])
        self.assertIn("@hr_anna", self.notes[0])
        self.assertEqual(self.state.applications["hash1"]["status"], "dry_run")
        self.assertEqual(self.sleeps, [])

    def test_dry_run_works_without_any_sender_configured(self):
        a = Applier(replace(self.cfg, apply_mode="dry_run"), self.state, senders={},
                    drafter=lambda e: draft(), notify=self.notes.append, resume_pdf=None,
                    ai_available=True)
        r = a.run([posting(1, "Hiring. Contact @hr_anna")], NOW)
        self.assertEqual(r.previewed, 1)

    def test_dry_run_records_do_not_block_going_live_for_new_contacts(self):
        self.applier(apply_mode="dry_run").run([posting(1, "Contact @hr_anna")], NOW)
        r = self.applier(apply_mode="auto").run([posting(2, "Contact @hr_anna")], NOW)
        self.assertEqual(r.sent, 1)  # dry-run cooldown is tracked separately


class AutoTests(ApplierTestCase):
    def test_sends_message_with_resume_and_confirms(self):
        r = self.applier().run([posting(1, "Hiring. Contact @hr_anna")], NOW)
        self.assertEqual(r.sent, 1)
        contact, message, _, attachment = self.tg.sent[0]
        self.assertEqual((contact, attachment), ("@hr_anna", self.resume))
        self.assertIn("Applied", self.notes[0])
        self.assertTrue(self.tg.closed)
        # Saved to disk immediately after sending (crash safety).
        saved = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(saved["applications"]["hash1"]["status"], "sent")

    def test_never_applies_twice_to_the_same_posting(self):
        self.applier().run([posting(1, "Contact @hr_anna")], NOW)
        r = self.applier(apply_contact_cooldown_days=0).run([posting(1, "Contact @hr_anna")], NOW)
        self.assertEqual((r.queued, r.sent), (0, 0))
        self.assertEqual(len(self.tg.sent), 1)

    def test_channel_preference_and_fallback_on_contact_error(self):
        self.tg.fail["@hr_anna"] = ContactError("privacy")
        r = self.applier().run([posting(1, "Contact @hr_anna or jobs@acme.io")], NOW)
        self.assertEqual(r.sent, 1)
        self.assertEqual(self.mail.sent[0][0], "jobs@acme.io")
        self.assertEqual(self.mail.sent[0][1], "Dear team, ...")  # email body, not the DM
        self.assertEqual(self.state.applications["hash1"]["channel"], "email")

    def test_all_channels_fail_records_failure(self):
        self.tg.fail["@hr_anna"] = ContactError("no such user")
        r = self.applier().run([posting(1, "Contact @hr_anna")], NOW)
        self.assertEqual((r.sent, r.failed), (0, 1))
        self.assertEqual(self.state.applications["hash1"]["status"], "failed")
        self.assertNotIn("hash1", self.state.apply_queue)

    def test_daily_cap_keeps_the_rest_queued(self):
        posts = [posting(i, f"Contact @hr_person{i}", age_days=i / 10) for i in range(5)]
        r = self.applier(apply_daily_cap=2).run(posts, NOW)
        self.assertEqual((r.sent, r.waiting), (2, 3))
        self.assertEqual([c for c, *_ in self.tg.sent], ["@hr_person0", "@hr_person1"])  # newest first
        self.assertEqual(self.sleeps, [45.0])  # one gap between the two sends
        later = self.applier(apply_daily_cap=2).run([], NOW + timedelta(hours=25))
        self.assertEqual((later.sent, later.waiting), (2, 1))

    def test_contact_cooldown(self):
        self.applier().run([posting(1, "Contact @hr_anna")], NOW)
        r = self.applier().run([posting(2, "Another role. Contact @HR_Anna")], NOW + timedelta(days=3))
        self.assertEqual((r.sent, r.skipped_cooldown), (0, 1))

    def test_low_fit_is_skipped(self):
        self.score = 40
        r = self.applier().run([posting(1, "Contact @hr_anna")], NOW)
        self.assertEqual((r.sent, r.skipped_fit), (0, 1))
        self.assertEqual(self.state.applications["hash1"]["score"], 40)

    def test_peer_flood_pauses_channel_and_uses_email(self):
        self.tg.fail["@hr_anna"] = ChannelUnavailable("PEER_FLOOD", pause_seconds=86400)
        r = self.applier().run([posting(1, "Contact @hr_anna or jobs@acme.io"),
                                posting(2, "Contact @hr_bobby", age_days=0.1)], NOW)
        self.assertEqual(r.sent, 1)                       # posting 1 went by email
        self.assertIn("hash2", self.state.apply_queue)    # telegram-only one waits
        self.assertTrue(any("paused" in n for n in self.notes))
        self.assertIn("telegram", self.state.apply_paused)
        # Still paused an hour later; resumes after 24h.
        self.assertEqual(self.applier().run([], NOW + timedelta(hours=1)).sent, 0)
        self.tg.fail.clear()
        self.assertEqual(self.applier().run([], NOW + timedelta(hours=25)).sent, 1)

    def test_auto_requires_the_resume_pdf(self):
        self.resume.unlink()
        r = self.applier().run([posting(1, "Contact @hr_anna")], NOW)
        self.assertEqual(r.sent, 0)
        self.assertIn("hash1", self.state.apply_queue)
        self.assertTrue(r.alerts)


class GateTests(ApplierTestCase):
    def test_no_contact_no_queue(self):
        self.assertEqual(self.applier().run([posting(1, "Hiring! Apply on our website")], NOW).queued, 0)

    def test_apply_keywords_and_excludes(self):
        a = self.applier(apply_keywords=["python"], apply_exclude_keywords=["senior"])
        r = a.run([posting(1, "Python dev. Contact @hr_anna"),
                   posting(2, "Go dev. Contact @hr_bobby"),
                   posting(3, "Senior Python dev. Contact @hr_carol")], NOW)
        self.assertEqual(r.queued, 1)
        self.assertEqual(self.tg.sent[0][0], "@hr_anna")

    def test_without_ai_apply_keywords_are_required(self):
        a = Applier(self.cfg, self.state, senders={"telegram": self.tg},
                    drafter=lambda e: template_draft(self.cfg, e), resume_pdf=self.resume,
                    ai_available=False, sleep=lambda s: None)
        self.assertEqual(a.run([posting(1, "Contact @hr_anna")], NOW).queued, 0)

    def test_old_postings_are_not_applied_to(self):
        r = self.applier(apply_max_age_days=7).run([posting(1, "Contact @hr_anna", age_days=10)], NOW)
        self.assertEqual(r.queued, 0)

    def test_twitter_handles_are_not_telegram_contacts(self):
        r = self.applier().run([posting(1, "We're hiring, DM @hr_anna", source_type="twitter")], NOW)
        self.assertEqual(r.queued, 0)

    def test_off_does_nothing(self):
        r = self.applier(apply_mode="off").run([posting(1, "Contact @hr_anna")], NOW)
        self.assertEqual((r.queued, r.sent), (0, 0))


class TemplateTests(unittest.TestCase):
    def test_template_fills_placeholders_and_keeps_unknown(self):
        cfg = Config(applicant_name="Mei", apply_template="Role {title} — {name} {unknown}")
        d = template_draft(cfg, {"title": "Go Dev"})
        self.assertEqual(d.message, "Role Go Dev — Mei {unknown}")
        self.assertIsNone(d.fit_score)
        self.assertEqual(d.email_subject, "Application: Go Dev")


if __name__ == "__main__":
    unittest.main()
