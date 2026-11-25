"""B5: the bot inbox (one confirmed reader of the bot's updates) and the
Telegram channel source (your account first, the bot as a fallback)."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from jobradar.collectors import telegram as tg
from jobradar.collectors.base import CollectorError, FetchContext
from jobradar.collectors.registry import REGISTRY
from jobradar.config import Config
from jobradar.delivery.inbox import BotInbox
from jobradar.pipeline import Pipeline
from jobradar.sites import resolve
from jobradar.state import State, utcnow

NOW = utcnow()


def post(update_id, msg_id, text, user="remote_jobs", chat_id=-1001):
    return {"update_id": update_id, "channel_post": {
        "chat": {"id": chat_id, "type": "channel", "title": "Remote Jobs", "username": user},
        "message_id": msg_id, "date": int((NOW - timedelta(minutes=5)).timestamp()), "text": text}}


class FakeTelegram:
    """Pending updates that disappear once confirmed (offset), like Telegram."""

    def __init__(self, updates):
        self.pending = list(updates)
        self.calls = []

    def __call__(self, inbox, method, params=None, timeout=None):
        self.calls.append((method, dict(params or {})))
        if method == "getUpdates":
            off = int(params.get("offset", 0))
            self.pending = [u for u in self.pending if u["update_id"] >= off]
            return self.pending[: int(params.get("limit", 100))]
        return True

    def patch(self):
        """Patch BotInbox.call with this fake (as a real method)."""
        fake = self
        return mock.patch.object(BotInbox, "call",
                                 lambda inbox, method, params=None, timeout=None: fake(inbox, method, params, timeout))


class InboxTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = State(Path(tmp.name) / "jobradar.db")
        self.inbox = BotInbox("123:abc", self.state)

    def poll(self, fake):
        with fake.patch():
            return self.inbox.poll()

    def test_backlog_over_100_is_fully_read(self):
        # The old reader never confirmed updates, so it saw the same 100 forever.
        fake = FakeTelegram([post(i, i, f"Hiring dev #{i}") for i in range(1, 251)])
        self.assertEqual([self.poll(fake) for _ in range(4)], [100, 100, 50, 0])
        self.assertEqual(len(self.state.bot_posts("remote_jobs")), 250)

    def test_chats_are_remembered_after_confirming(self):
        fake = FakeTelegram([{"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Mei"}}}])
        self.poll(fake)
        self.poll(fake)  # nothing new
        self.assertEqual([c["id"] for c in self.state.bot_chats()], ["42"])

    def test_offset_survives_a_restart(self):
        self.poll(FakeTelegram([post(7, 1, "Hiring")]))
        fake = FakeTelegram([])
        with fake.patch():
            BotInbox("123:abc", self.state).poll()
        self.assertEqual(fake.calls[0][1]["offset"], 8)

    def test_buttons_go_to_their_handler(self):
        seen = []
        self.inbox.on_button("rv:", lambda cq: seen.append(cq["data"]) or "Saved")
        fake = FakeTelegram([{"update_id": 1, "callback_query": {"id": "q1", "data": "rv:5:y"}}])
        self.poll(fake)
        self.assertEqual(seen, ["rv:5:y"])
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q1", "text": "Saved"}), fake.calls)

    def test_one_odd_update_doesnt_block_the_rest(self):
        bad = {"update_id": 1, "channel_post": {"chat": {"id": 1}, "message_id": "x", "date": "y", "text": "t"}}
        self.poll(FakeTelegram([bad, post(2, 2, "Hiring a Go dev")]))
        self.assertEqual(len(self.state.bot_posts("remote_jobs")), 1)


class ChannelCollectorTests(unittest.TestCase):
    def collector(self, **cfg):
        return tg.TelegramChannelCollector(name="tg-remote_jobs", tier="A",
                                           config={"channel": "remote_jobs", **cfg}, http=None)

    def ctx(self, bot_posts=None):
        return FetchContext(since=NOW - timedelta(days=1), now=NOW, user_agent="t", bot_posts=bot_posts)

    def test_reads_public_channel_with_your_account(self):
        msgs = [(11, NOW - timedelta(hours=1), "We're hiring a Rust developer")]
        with mock.patch.object(tg, "_account_messages", return_value=msgs):
            items = list(self.collector().fetch(self.ctx()))
        self.assertEqual((items[0].url, items[0].raw_text), ("https://t.me/remote_jobs/11", msgs[0][2]))

    def test_falls_back_to_bot_posts(self):
        rows = [{"message_id": 5, "posted_at": (NOW - timedelta(hours=2)).isoformat(), "text": "Hiring"}]
        with mock.patch.object(tg, "_account_messages", return_value=None):
            items = list(self.collector().fetch(self.ctx(bot_posts=lambda ch, since: rows)))
        self.assertEqual(items[0].external_id, "remote_jobs/5")

    def test_says_what_to_set_up(self):
        with mock.patch.object(tg, "_account_messages", return_value=None), self.assertRaises(CollectorError) as cm:
            list(self.collector().fetch(self.ctx()))
        self.assertIn("log in your Telegram account", str(cm.exception))

    def test_account_not_set_up_means_none(self):
        self.assertIsNone(tg._account_messages({}, "remote_jobs", None))
        self.assertIsNone(tg._account_messages({"telegram_api_id": "1", "telegram_api_hash": "h",
                                                "telegram_session_path": "/nonexistent/x.session"}, "c", None))


class SitesTests(unittest.TestCase):
    def test_channel_links(self):
        got = resolve(["t.me/remote_jobs", "https://t.me/s/GoJobs", "@rust_jobs", "t.me/joinchat/AAAA", "t.me/+x"])
        self.assertEqual([(s["type"], s["channel"]) for s in got],
                         [("telegram", "remote_jobs"), ("telegram", "GoJobs"), ("telegram", "rust_jobs")])


class PipelineTests(unittest.TestCase):
    def test_channel_posts_reach_you_through_the_bot(self):
        class T:
            def __init__(self):
                self.messages = []

            def send_message(self, chat, text):
                self.messages.append(text)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(tg, "_account_messages", return_value=None), \
                mock.patch.dict(REGISTRY, {"telegram": tg.TelegramChannelCollector}):
            state = State(Path(d) / "jobradar.db")
            inbox = BotInbox("123:abc", state)
            fake = FakeTelegram([post(1, 77, "We're hiring a remote backend engineer. Python. DM @hr_anna")])
            t = T()
            with fake.patch():
                s = Pipeline(Config(keywords=[], telegram_chat_id="1", message_interval_seconds=0), state,
                             [{"name": "tg-remote_jobs", "type": "telegram", "channel": "remote_jobs"}],
                             transport=t, inbox=inbox).run()
        self.assertEqual(s.delivered, 1)
        self.assertIn("https://t.me/remote_jobs/77", t.messages[0])


if __name__ == "__main__":
    unittest.main()
