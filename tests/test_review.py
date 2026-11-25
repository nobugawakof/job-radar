"""S1: the review queue for jobs that don't say where they hire (SRS FR-20..26)."""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar.collectors.registry import REGISTRY
from jobradar.config import Config, load
from jobradar.delivery.inbox import BotInbox, stop_all
from jobradar.models import RawItem
from jobradar.service import Service
from jobradar.state import iso, utcnow

NO_PLACE = "We're hiring a remote backend engineer. Python, Postgres. DM @hr_anna"
NO_PLACE_2 = "Hiring a frontend developer with React and TypeScript experience. Apply via our site."
IN_GERMANY = "We're hiring a remote backend engineer. Remote (EU only). Go, Kafka."


def collector(texts):
    class Src:
        type = "rss"

        def __init__(self, **kw):
            pass

        def fetch(self, ctx):
            for i, t in enumerate(texts):
                yield RawItem(source="jobs", external_id=str(i), raw_text=t, url=f"https://x/{i}",
                              posted_at=utcnow() - timedelta(hours=1))
    return Src


class FakeTransport:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_message(self, chat, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram is down")
        self.sent.append((text, reply_markup))


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(stop_all)
        self.dir = Path(tmp.name)
        self.calls = []
        patcher = mock.patch.object(BotInbox, "call", lambda inbox, method, params=None, timeout=None:
                                    self.calls.append((method, params)) or ([] if method == "getUpdates" else True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self, texts, **cfg):
        svc = Service(Config(keywords=[], regions=["Germany"], telegram_bot_token="1:x", telegram_chat_id="42",
                             message_interval_seconds=0, **cfg), config_path=str(self.dir / "config.toml"))
        self.t = FakeTransport()
        svc.transport = svc.pipeline.transport = self.t
        svc.pipeline.source_defs = [{"name": "jobs", "type": "rss"}]
        self.registry = mock.patch.dict(REGISTRY, {"rss": collector(texts)})
        self.registry.start()
        self.addCleanup(self.registry.stop)
        return svc


class QueueTests(ReviewTestCase):
    def test_unclear_jobs_wait_for_you_and_are_asked_about_once(self):
        svc = self.service([NO_PLACE, NO_PLACE_2, IN_GERMANY])
        s = svc.run()
        self.assertEqual((s.delivered, s.queued_review), (1, 2))  # only the EU job is sent
        prompts = [m for m in self.t.sent if m[1]]
        self.assertEqual(len(prompts), 1)                            # one batched message (FR-26)
        text, markup = prompts[0]
        self.assertIn("2 jobs don't say where they hire", text)
        self.assertEqual(len(markup["inline_keyboard"]), 2)
        s2 = svc.run()
        self.assertEqual((s2.delivered, s2.queued_review), (0, 0))   # not queued or asked again
        self.assertEqual(len(svc.state.review_pending()), 2)

    def test_approve_sends_the_job(self):
        svc = self.service([NO_PLACE])
        svc.run()
        item = svc.state.review_pending()[0]
        self.assertEqual(svc.review_decide(item["id"], True, "dashboard"), "Sent ✅")
        card = self.t.sent[-1][0]
        self.assertIn("Location: not stated", card)
        self.assertTrue(svc.state.already_sent(item["fingerprint"]))
        self.assertEqual(svc.state.recent_jobs(1)[0]["url"], "https://x/0")
        self.assertEqual(svc.review_decide(item["id"], True, "telegram"), "Already handled.")  # no double send
        self.assertEqual(len([m for m in self.t.sent if "Location: not stated" in m[0]]), 1)

    def test_reject_is_final(self):
        svc = self.service([NO_PLACE])
        svc.run()
        item = svc.state.review_pending()[0]
        self.assertEqual(svc.review_decide(item["id"], False, "dashboard"), "Skipped.")
        svc.run()
        self.assertEqual(svc.state.review_pending(), [])

    def test_failed_send_keeps_it_waiting(self):
        svc = self.service([NO_PLACE])
        svc.run()
        item = svc.state.review_pending()[0]
        svc.pipeline.transport = FakeTransport(fail=True)
        self.assertIn("Couldn't send", svc.review_decide(item["id"], True, "dashboard"))
        self.assertEqual(len(svc.state.review_pending()), 1)

    def test_telegram_button(self):
        svc = self.service([NO_PLACE, NO_PLACE_2])
        svc.run()
        markup = [m for m in self.t.sent if m[1]][0][1]
        first = markup["inline_keyboard"][0][0]["callback_data"]            # "rv:<id>:y"
        note = svc._review_button({"id": "q", "data": first, "message": {
            "message_id": 9, "chat": {"id": 42}, "reply_markup": markup}})
        self.assertEqual(note, "Sent ✅")
        edit = [p for m, p in self.calls if m == "editMessageReplyMarkup"][0]
        self.assertEqual(len(edit["reply_markup"]["inline_keyboard"]), 1)    # that row is gone
        self.assertEqual(len(svc.state.review_pending()), 1)

    def test_unanswered_jobs_expire(self):
        svc = self.service([NO_PLACE])
        svc.run()
        with svc.state._tx() as db:
            db.execute("UPDATE review_queue SET added_at = ?", (iso(utcnow() - timedelta(days=15)),))
        s = svc.run()
        self.assertEqual((svc.state.review_pending(), s.queued_review), ([], 0))
        self.assertEqual(svc.state.review_counts().get("expired"), 1)

    def test_other_modes(self):
        svc = self.service([NO_PLACE], unknown_location="send")
        self.assertEqual(svc.run().delivered, 1)
        svc = self.service([NO_PLACE_2], unknown_location="drop")
        s = svc.run()
        self.assertEqual((s.delivered, s.queued_review, s.rejected_region), (0, 0, 1))


class SuggestionTests(ReviewTestCase):
    def test_repeated_skips_suggest_switching_the_source_off(self):
        from jobradar.web.app import Api, Controller

        (self.dir / "config.toml").write_text('regions = ["Germany"]\n', "utf-8")
        ctrl = Controller(self.dir / "config.toml")
        st = ctrl.service.state
        for i in range(5):
            item_id = st.review_add(f"fp{i}", {"title": f"Job {i}", "source": "reddit-forhire",
                                              "origins": ["reddit-forhire"]})
            st.review_resolve(item_id, "rejected", "dashboard")
        out = Api(ctrl).review_list(None)
        self.assertIn("reddit-forhire", out["suggestions"][0]["text"])


class LegacyConfigTests(unittest.TestCase):
    def test_old_switch_is_carried_over(self):
        with tempfile.TemporaryDirectory() as d:
            for old, new in ((False, "drop"), (True, "send")):
                p = Path(d) / "config.toml"
                p.write_text(f"send_unknown_location = {json.dumps(old)}\n", "utf-8")
                self.assertEqual(load(p).unknown_location, new)
            p.write_text('unknown_location = "review"\nsend_unknown_location = false\n', "utf-8")
            self.assertEqual(load(p).unknown_location, "review")


if __name__ == "__main__":
    unittest.main()
