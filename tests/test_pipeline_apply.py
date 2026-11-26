import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar import ai
from jobradar.ai import ApplicationDraft
from jobradar.apply.engine import Applier
from jobradar.collectors.registry import REGISTRY
from jobradar.config import Config
from jobradar.models import RawItem
from jobradar.pipeline import Pipeline
from jobradar.state import State, utcnow

POSTS = [
    "We're hiring a remote Python backend engineer. Salary $100k. Contact @hr_anna",
    "We're hiring a remote Python backend engineer for payments. Apply: jobs@acme.io",
    "Looking for a job as a python developer, hire me!",   # seeker: never a posting
]


class FakeTelegramCollector:
    """Stands in for the real "telegram" collector (registered per test), so
    the pipeline tags postings with source_type="telegram"."""

    type = "telegram"

    def __init__(self, *, name, tier, config, http):
        self.name, self.tier = name, tier

    def fetch(self, ctx):
        for i, text in enumerate(POSTS):
            yield RawItem(source=self.name, external_id=str(i), raw_text=text,
                          url=f"https://t.me/jobs/{i}", posted_at=utcnow() - timedelta(hours=i + 1))


class FakeTransport:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append(text)
        return {}


class FakeSender:
    def __init__(self, channel):
        self.channel, self.sent = channel, []

    def send(self, contact, *, message, subject, attachment):
        self.sent.append(contact)

    def close(self):
        pass


class PipelineApplyTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(REGISTRY, {"telegram": FakeTelegramCollector})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_delivered_postings_are_applied_to(self):
        with tempfile.TemporaryDirectory() as d:
            resume = Path(d) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4")
            cfg = Config(keywords=["python"], telegram_chat_id="1", message_interval_seconds=0, message_language="en",
                         apply_mode="auto", apply_delay_seconds=[0, 0])
            state = State(Path(d) / "jobradar.db")
            transport = FakeTransport()
            tg, mail = FakeSender("telegram"), FakeSender("email")
            applier = Applier(cfg, state, senders={"telegram": tg, "email": mail},
                              drafter=lambda e: ApplicationDraft(90, "fit", "Hi", "Subj", "Body"),
                              notify=lambda t: transport.send_message("1", t),
                              resume_pdf=resume, ai_available=True, sleep=lambda s: None)
            sources = [{"name": "tg-jobs", "type": "telegram", "tier": "A"}]
            summary = Pipeline(cfg, state, sources, transport=transport, applier=applier).run()

            self.assertEqual(summary.delivered, 2)
            self.assertEqual(summary.apply.sent, 2)
            self.assertEqual(tg.sent, ["@hr_anna"])
            self.assertEqual(mail.sent, ["jobs@acme.io"])
            # 2 job cards + 2 "Applied" confirmations
            self.assertEqual(sum("Applied" in m for m in transport.messages), 2)
            self.assertEqual(len(State(Path(d) / "jobradar.db").list_applications()), 2)
            # Delivered jobs are stored with their full text.
            jobs = state.recent_jobs(with_text=True)
            self.assertEqual(len(jobs), 2)
            self.assertIn("Python backend engineer", jobs[0]["description"])

            # A second run re-fetches the same posts: nothing is sent or applied again.
            again = Pipeline(cfg, state, sources, transport=transport, applier=applier).run()
            self.assertEqual((again.delivered, again.apply.sent), (0, 0))


class AiRequestShapeTests(unittest.TestCase):
    def _capture(self, reply):
        cap = {}

        def fake(url, body, headers, timeout):
            cap.update(url=url, body=body, headers=headers)
            return reply

        return cap, fake

    def test_claude_draft_with_pdf_resume_and_fallbacks(self):
        reply = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({
            "fit_score": 130, "fit_reason": "r", "language": "en", "message": "Hi",
            "email_subject": "S", "email_body": ""})}]}
        cap, fake = self._capture(reply)
        with mock.patch.object(ai, "_post_json", fake):
            d = ai.draft_application("Hiring", resume_pdf=b"%PDF", api_key="k", model="claude-opus-5-5")
        self.assertEqual(d.fit_score, 100)          # clamped
        self.assertEqual(d.email_body, "Hi")        # falls back to the message
        self.assertEqual(cap["body"]["messages"][0]["content"][0]["type"], "document")
        self.assertEqual(cap["body"]["fallbacks"], "default")
        self.assertEqual(cap["headers"]["anthropic-beta"], "server-side-fallback-2026-07-01")

    def test_refusal_returns_none(self):
        cap, fake = self._capture({"stop_reason": "refusal", "content": []})
        with mock.patch.object(ai, "_post_json", fake):
            self.assertIsNone(ai.draft_application("x", resume_text="cv", api_key="k"))

    def test_haiku_4_5_gets_no_effort(self):
        cap, fake = self._capture({"stop_reason": "end_turn", "content": []})
        with mock.patch.object(ai, "_post_json", fake):
            ai.enrich("Hiring dev", api_key="k", model="claude-haiku-4-5")
        self.assertNotIn("effort", cap["body"]["output_config"])
        self.assertNotIn("fallbacks", cap["body"])

    def test_gemini_draft_with_text_resume(self):
        reply = {"candidates": [{"content": {"parts": [{"text": json.dumps({
            "fit_score": 55, "message": "你好", "email_subject": "s", "email_body": "b"})}]}}]}
        cap, fake = self._capture(reply)
        with mock.patch.object(ai, "_post_json", fake):
            d = ai.draft_application("招聘", resume_text="cv", api_key="k", provider="gemini",
                                     model="gemini-2.5-flash")
        self.assertEqual((d.fit_score, d.message), (55, "你好"))
        self.assertIn("<resume>", cap["body"]["contents"][0]["parts"][0]["text"])

    def test_no_resume_means_no_ai_call(self):
        with mock.patch.object(ai, "_post_json") as m:
            self.assertIsNone(ai.draft_application("Hiring", api_key="k"))
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
