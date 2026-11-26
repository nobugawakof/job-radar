"""Conversations with representatives (one per account, mirrored from your
Telegram account), replies, and Chinese/English messages (Chinese default)."""

import json
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Thread
from unittest import mock

from jobradar import ai, tgclient
from jobradar.ai import ApplicationDraft
from jobradar.apply import conversations, language
from jobradar.apply.engine import Applier, template_draft
from jobradar.apply.senders import SendResult
from jobradar.config import Config
from jobradar.models import Posting
from jobradar.state import State, utcnow
from jobradar.web import app as web

NOW = utcnow()


class LanguageTests(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(language.resolve(None, "anything"), "zh")             # default: Chinese
        self.assertEqual(language.resolve("en", "招聘后端工程师"), "en")
        self.assertEqual(language.resolve("auto", "招聘后端工程师，远程，薪资面议"), "zh")
        self.assertEqual(language.resolve("auto", "We're hiring a backend engineer"), "en")

    def test_template_is_chinese_by_default(self):
        d = template_draft(Config(applicant_name="陈美"), {"title": "后端工程师", "description": "Hiring"})
        self.assertIn("您好", d.message)
        self.assertIn("「后端工程师」", d.message)
        self.assertEqual(d.email_subject, "应聘：后端工程师")
        en = template_draft(Config(message_language="en", applicant_name="Mei"), {"title": "Go Dev"})
        self.assertTrue(en.message.startswith("Hi!"))

    def test_ai_is_told_the_language(self):
        seen = {}

        def fake(url, body, headers, timeout):
            seen["system"] = body["system"]
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(
                {"fit_score": 80, "fit_reason": "", "language": "zh", "message": "您好", "email_subject": "s",
                 "email_body": "b"})}]}

        with mock.patch.object(ai, "_post_json", fake):
            ai.draft_application("We're hiring", resume_text="cv", api_key="k")
            self.assertIn("简体中文", seen["system"])
            ai.draft_application("We're hiring", resume_text="cv", api_key="k", language="en")
            self.assertIn("in English, whatever language", seen["system"])
            ai.draft_application("We're hiring", resume_text="cv", api_key="k", language="auto")
            self.assertIn("same language as the posting", seen["system"])


class FakeSender:
    def __init__(self, channel):
        self.channel = channel
        self.sent = []

    def send(self, contact, *, message, subject, attachment):
        self.sent.append((contact, message))
        return SendResult(peer_id="9001", display_name="Anna Li", text_id="101", file_id="102")

    def close(self):
        pass


class RecordingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.resume = self.dir / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4")
        self.state = State(self.dir / "jobradar.db")
        self.notes = []

    def applier(self, mode="auto"):
        cfg = Config(apply_mode=mode, apply_delay_seconds=[0, 0])
        return Applier(cfg, self.state, senders={"telegram": FakeSender("telegram")},
                       drafter=lambda e: ApplicationDraft(85, "good", "您好，我想应聘…", "应聘", "您好"),
                       notify=self.notes.append, resume_pdf=self.resume, ai_available=True, sleep=lambda s: None)

    def posting(self):
        return Posting(title="后端工程师", description="招聘后端工程师，远程。联系 @hr_anna", source="tg",
                       source_tier="A", source_url="https://t.me/jobs/1", posted_at=NOW, collected_at=NOW,
                       content_hash="fp1", source_type="telegram")

    def test_sent_application_starts_a_conversation(self):
        self.applier().run([self.posting()], NOW)
        convs = self.state.conversations()
        self.assertEqual((convs[0]["contact"], convs[0]["display_name"], convs[0]["peer_id"]),
                         ("@hr_anna", "Anna Li", "9001"))
        msgs = self.state.conversation(convs[0]["id"])["messages"]
        self.assertEqual([(m["direction"], m["text"], m["attachment"], m["external_id"]) for m in msgs],
                         [("out", "您好，我想应聘…", "", "101"), ("out", "", "resume.pdf", "102")])
        self.assertEqual(msgs[0]["job_title"], "后端工程师")
        self.assertEqual(self.state.get_application("fp1")["message"], "您好，我想应聘…")
        self.assertIn("已投递", self.notes[0])                     # notice in Chinese

    def test_dry_run_keeps_the_draft_but_starts_no_conversation(self):
        self.applier("dry_run").run([self.posting()], NOW)
        self.assertEqual(self.state.conversations(), [])
        self.assertEqual(self.state.get_application("fp1")["message"], "您好，我想应聘…")
        self.assertIn("演练", self.notes[0])


class _Msg:
    def __init__(self, mid, out, text, file=None):
        self.id, self.out, self.message, self.date = mid, out, text, NOW - timedelta(minutes=100 - mid)
        self.file = type("F", (), {"name": file})() if file else None
        self.photo = None


class FakeClient:
    def __init__(self, history):
        self.history = history  # target -> list of _Msg (oldest first)

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        pass

    def iter_messages(self, target, limit=50, min_id=0):
        msgs = [m for m in reversed(self.history.get(target, [])) if m.id > min_id][:limit]

        async def gen():
            for m in msgs:
                yield m
        return gen()


class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "me.session").write_text("x")
        self.cfg = Config(telegram_api_id="1", telegram_api_hash="h", telegram_session_path=str(self.dir / "me.session"))
        self.state = State(self.dir / "jobradar.db")
        self.conv = self.state.conversation_id("telegram", "@hr_anna", display_name="Anna Li", peer_id="9001")
        self.state.add_message(self.conv, "out", "您好，我想应聘…", external_id="101", job_title="后端工程师")
        self.history = {9001: [_Msg(101, True, "您好，我想应聘…"), _Msg(102, True, "", "resume.pdf"),
                               _Msg(103, False, "你好！简历收到了，周三方便聊一下吗？"),
                               _Msg(104, True, "方便，周三下午可以。")]}  # sent from your phone

    def sync(self, notes=None):
        with mock.patch.object(tgclient, "make_client", lambda runner, *a: FakeClient(self.history)):
            return conversations.sync_telegram(self.cfg, self.state, notify=(notes.append if notes is not None else None))

    def test_replies_and_phone_messages_are_mirrored(self):
        notes = []
        self.assertEqual(self.sync(notes), {"synced": 1, "new_replies": 1})
        msgs = self.state.conversation(self.conv)["messages"]
        self.assertEqual([(m["direction"], m["text"] or m["attachment"]) for m in msgs], [
            ("out", "您好，我想应聘…"), ("out", "resume.pdf"),
            ("in", "你好！简历收到了，周三方便聊一下吗？"), ("out", "方便，周三下午可以。")])
        self.assertEqual(self.state.unread_total(), 1)
        self.assertIn("Anna Li", notes[0])
        self.assertIn("回复了你（后端工程师）", notes[0])          # Chinese notice by default
        self.assertEqual(self.sync(), {"synced": 1, "new_replies": 0})   # nothing twice
        self.assertEqual(len(self.state.conversation(self.conv)["messages"]), 4)

    def test_busy_account_is_reported(self):
        holder = threading.Thread(target=lambda: (tgclient.ACCOUNT_LOCK.acquire(), done.wait(),
                                                  tgclient.ACCOUNT_LOCK.release()))
        done = threading.Event()
        holder.start()
        try:
            r = conversations.sync_telegram(self.cfg, self.state, lock_timeout=0.1)
            self.assertIn("busy", r["error"])
        finally:
            done.set()
            holder.join()

    def test_not_logged_in(self):
        r = conversations.sync_telegram(replace(self.cfg, telegram_session_path="/nope"), self.state)
        self.assertIn("not logged in", r["error"])


class ReplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = State(self.dir / "jobradar.db")

    def test_reply_by_telegram(self):
        conv = self.state.conversation_id("telegram", "@hr_anna")
        fake = FakeSender("telegram")
        with mock.patch("jobradar.apply.engine.build_senders", return_value={"telegram": fake}):
            out = conversations.send_reply(Config(), self.state, self.dir, conv, "谢谢，周三见！")
        self.assertEqual(fake.sent, [("@hr_anna", "谢谢，周三见！")])
        self.assertEqual(out["messages"][-1]["text"], "谢谢，周三见！")
        self.assertEqual(out["peer_id"], "9001")

    def test_reply_by_email_keeps_the_subject(self):
        conv = self.state.conversation_id("email", "jobs@acme.io")
        self.state.add_message(conv, "out", "您好", subject="应聘：后端工程师")
        seen = {}

        class Mail(FakeSender):
            def send(self, contact, *, message, subject, attachment):
                seen["subject"] = subject
                return SendResult(text_id="<x@me>")

        with mock.patch("jobradar.apply.engine.build_senders", return_value={"email": Mail("email")}):
            conversations.send_reply(Config(), self.state, self.dir, conv, "补充一下我的作品集链接")
        self.assertEqual(seen["subject"], "Re: 应聘：后端工程师")

    def test_reply_needs_a_way_to_send(self):
        conv = self.state.conversation_id("telegram", "@hr_anna")
        with self.assertRaises(conversations.ReplyError):
            conversations.send_reply(Config(), self.state, self.dir, conv, "hi")


class DashboardTests(unittest.TestCase):
    def test_list_open_and_reply(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text("", "utf-8")
            server = web.make_server(cfg, port=0)
            Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            try:
                st = server.ctrl.service.state
                conv = st.conversation_id("telegram", "@hr_anna", display_name="Anna Li")
                st.add_message(conv, "in", "你好！", external_id="5", unread=True)
                base = f"http://127.0.0.1:{server.server_address[1]}"
                h = {"Cookie": f"{web.COOKIE}={server.token}", "X-Requested-With": "jobradar",
                     "Content-Type": "application/json"}

                def call(method, path, body=None):
                    r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None,
                                               method=method, headers=h)
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        return json.loads(resp.read())

                lst = call("GET", "/api/messages")
                self.assertEqual((lst["unread"], lst["conversations"][0]["display_name"]), (1, "Anna Li"))
                opened = call("POST", "/api/messages/open", {"id": conv})
                self.assertEqual(opened["conversation"]["messages"][0]["text"], "你好！")
                self.assertEqual(call("GET", "/api/state")["unread_messages"], 0)
                with mock.patch("jobradar.apply.engine.build_senders", return_value={"telegram": FakeSender("telegram")}):
                    sent = call("POST", "/api/messages/send", {"id": conv, "text": "你好，我是陈美"})
                self.assertEqual(sent["conversation"]["messages"][-1]["direction"], "out")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
