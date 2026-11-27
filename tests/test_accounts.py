"""Accounts: sign-up and login, the administrator (kof311), roles, keeping
users' data apart, restricting/deleting accounts, and per-user alerts and
auto-apply."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from unittest import mock

from jobradar import accounts as acc
from jobradar import auth
from jobradar.ai import ApplicationDraft
from jobradar.apply.senders import SendResult
from jobradar.config import Config
from jobradar.service import EveryoneApplier, Service
from jobradar.state import utcnow
from jobradar.web import app as web

PROFILE = {"name": "Mei Chen", "job_title": "Backend Engineer", "education": [{"school": "MIT"}]}


class AuthHelperTests(unittest.TestCase):
    def test_passwords(self):
        h = auth.hash_password("correct horse")
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertNotIn("correct horse", h)
        self.assertTrue(auth.verify_password("correct horse", h))
        self.assertFalse(auth.verify_password("wrong", h))
        self.assertFalse(auth.verify_password("x", ""))
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))   # salted

    def test_names(self):
        self.assertEqual(auth.check_username(" mei.chen "), "mei.chen")
        for bad in ("kof311", "KOF311", "Admin", "ab", "has space", "-start", "x" * 40):
            with self.assertRaises(auth.AuthError):
                auth.check_username(bad)
        with self.assertRaises(auth.AuthError):
            auth.check_password("short")

    def test_throttle(self):
        t = auth.Throttle(free=3, base=10)
        for i in range(3):
            self.assertEqual(t.wait("k", now=100 + i), 0)
            t.failed("k", now=100 + i)
        self.assertGreater(t.wait("k", now=103), 0)
        self.assertEqual(t.wait("other", now=103), 0)
        t.clear("k")
        self.assertEqual(t.wait("k", now=103), 0)


class Server(unittest.TestCase):
    """A running dashboard and a small HTTP client."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.cfg = self.dir / "config.toml"
        self.cfg.write_text('telegram_api_id = "1"\ntelegram_api_hash = "h"\nmessage_language = "en"\n', "utf-8")
        self.server = web.make_server(self.cfg, port=0)
        Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.state = self.server.ctrl.service.state

    def call(self, method, path, body=None, *, cookie=None, raw=False):
        h = {"X-Requested-With": "jobradar", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = f"{web.COOKIE}={cookie}"
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                data = resp.read()
                return resp.status, (data if raw else json.loads(data)), resp.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else json.loads(data)), e.headers

    @property
    def admin(self):
        return self.server.token     # the printed link's secret = the administrator, on this computer

    def signup(self, name, password="password123"):
        status, body, headers = self.call("POST", "/api/auth/signup", {"username": name, "password": password})
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]


class SignupLoginTests(Server):
    def test_signup_logs_in_at_once(self):
        cookie = self.signup("mei")
        status, me, _ = self.call("GET", "/api/auth/me", cookie=cookie)
        self.assertEqual((me["user"]["username"], me["user"]["role"], me["user"]["is_admin"]), ("mei", "user", False))
        self.assertIn("HttpOnly", self.call("POST", "/api/auth/signup", {"username": "bob", "password": "password123"})[2]["Set-Cookie"])
        # the stored session is a hash, never the cookie itself
        self.assertIsNone(self.state._one("SELECT 1 FROM sessions WHERE token_hash = ?", (cookie,)))

    def test_reserved_and_taken_names(self):
        for name in ("kof311", "KOF311"):
            status, body, _ = self.call("POST", "/api/auth/signup", {"username": name, "password": "password123"})
            self.assertEqual(status, 400)
            self.assertIn("reserved", body["error"])
        self.signup("mei")
        status, body, _ = self.call("POST", "/api/auth/signup", {"username": "MEI", "password": "password123"})
        self.assertEqual((status, body["error"]), (409, "That username is taken."))

    def test_login_logout_and_throttle(self):
        self.signup("mei")
        status, body, headers = self.call("POST", "/api/auth/login", {"username": "Mei", "password": "password123"})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 200)
        self.call("POST", "/api/auth/logout", {}, cookie=cookie)
        self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 401)
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/auth/login", {"username": "mei", "password": "nope"})[0], 401)
        status, body, _ = self.call("POST", "/api/auth/login", {"username": "mei", "password": "password123"})
        self.assertEqual(status, 429)                         # slowed down after repeated failures
        self.assertIn("Too many attempts", body["error"])

    def test_first_admin_login_sets_the_password(self):
        # kof311 has no password yet: the first login as kof311, from any computer, chooses it
        self.assertTrue(self.call("GET", "/api/auth/me")[1]["admin_unclaimed"])
        status, body, headers = self.call("POST", "/api/auth/login", {"username": "kof311", "password": "admin-secret-1"})
        self.assertEqual((status, body["claim"]), (200, True))           # asks to type it twice
        self.assertNotIn("Set-Cookie", headers)
        status, body, _ = self.call("POST", "/api/auth/login",
                                    {"username": "kof311", "password": "admin-secret-1", "confirm": "typo"})
        self.assertEqual(status, 400)
        status, body, _ = self.call("POST", "/api/auth/login", {"username": "KOF311", "password": "short", "confirm": "short"})
        self.assertEqual(status, 400)                                     # still the password rules
        status, body, headers = self.call("POST", "/api/auth/login",
                                          {"username": "kof311", "password": "admin-secret-1", "confirm": "admin-secret-1"})
        self.assertEqual((status, body["claimed"], body["user"]["is_admin"]), (200, True, True))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertFalse(self.call("GET", "/api/auth/me")[1]["admin_unclaimed"])
        # from now on it's a normal login: nobody can claim it again
        status, _, _ = self.call("POST", "/api/auth/login",
                                 {"username": "kof311", "password": "other-pass-2", "confirm": "other-pass-2"})
        self.assertEqual(status, 401)
        self.assertEqual(self.call("POST", "/api/auth/login", {"username": "kof311", "password": "admin-secret-1"})[0], 200)

    def test_admin_password_set_on_this_computer(self):
        status, body, _ = self.call("POST", "/api/auth/password", {"password": "admin-secret-1"}, cookie=self.admin)
        self.assertEqual(status, 200, body)
        status, body, _ = self.call("POST", "/api/auth/login", {"username": "kof311", "password": "admin-secret-1"})
        self.assertEqual((status, body["user"]["is_admin"]), (200, True))
        # changing it again needs the current one
        cookie = self.signup("mei")
        status, body, _ = self.call("POST", "/api/auth/password", {"current": "wrong", "password": "newpassword1"},
                                    cookie=cookie)
        self.assertEqual(status, 403)


class RoleTests(Server):
    def test_users_only_reach_their_own_pages(self):
        cookie = self.signup("mei")
        status, st, _ = self.call("GET", "/api/state", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(st["me"]["username"], "mei")
        for secret in ("config", "secrets", "sources", "network"):
            self.assertNotIn(secret, st)                      # no settings or keys for regular users
        for method, path, body in (("POST", "/api/config", {"changes": {}}), ("POST", "/api/run", {}),
                                   ("GET", "/api/users", None), ("POST", "/api/ai/test", {}),
                                   ("GET", "/api/review", None), ("GET", "/api/backup", None),
                                   ("GET", "/api/logs", None), ("POST", "/api/users/create", {})):
            status, body, _ = self.call(method, path, body, cookie=cookie, raw=True)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.call("GET", "/api/users", cookie=self.admin)[0], 200)
        self.assertEqual(self.call("GET", "/api/state")[0], 401)

    def test_data_stays_apart(self):
        a, b = self.signup("anna"), self.signup("bob")
        self.assertEqual(self.call("POST", "/api/profile", {"profile": PROFILE}, cookie=a)[0], 200)
        self.assertEqual(self.call("GET", "/api/profile", cookie=b)[1]["profile"]["name"], "")
        uid_a = self.state.user_by_name("anna")["id"]
        sa = self.state.for_user(uid_a)
        sa.finish_application("fp1", {"at": "2026-01-01T00:00:00+00:00", "mode": "auto", "status": "sent",
                                      "title": "Go dev", "resume_file": f"resumes/u{uid_a}/fp1/x.pdf"})
        conv = sa.conversation_id("telegram", "@hr")
        self.assertEqual(len(self.call("GET", "/api/applications", cookie=a)[1]["applications"]), 1)
        self.assertEqual(self.call("GET", "/api/applications", cookie=b)[1]["applications"], [])
        allapps = self.call("GET", "/api/applications", cookie=self.admin)[1]
        self.assertEqual((allapps["all_users"], allapps["applications"][0]["username"]), (True, "anna"))
        self.assertEqual(self.call("POST", "/api/messages/open", {"id": conv}, cookie=b)[0], 404)
        self.assertEqual(self.call("POST", "/api/messages/open", {"id": conv}, cookie=a)[0], 200)
        pdf = self.dir / "resumes" / f"u{uid_a}" / "fp1" / "x.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-1.4")
        path = f"/api/file?path=resumes/u{uid_a}/fp1/x.pdf"
        self.assertEqual(self.call("GET", path, cookie=a, raw=True)[0], 200)
        self.assertEqual(self.call("GET", path, cookie=b, raw=True)[0], 404)
        self.assertEqual(self.call("GET", path, cookie=self.admin, raw=True)[0], 200)

    def test_each_user_logs_in_their_own_telegram(self):
        cookie = self.signup("mei")
        uid = self.state.user_by_name("mei")["id"]
        seen = {}

        def start(self_, **kw):
            seen.update(kw)
            return {"stage": "code"}

        with mock.patch.object(web.TelegramLogin, "start", start):
            status, body, _ = self.call("POST", "/api/tg/start", {"phone": "+60123456789"}, cookie=cookie)
        self.assertEqual((status, body["stage"]), (200, "code"))
        self.assertEqual(seen["session_path"], str(self.dir / "sessions" / f"user-{uid}.session"))
        self.assertNotIn("telegram_phone", self.cfg.read_text())            # a user's phone isn't saved in settings


class AdminTests(Server):
    def test_create_restrict_and_delete(self):
        status, body, _ = self.call("POST", "/api/users/create", {"username": "carol", "password": "password123"},
                                    cookie=self.admin)
        self.assertEqual([u["username"] for u in body["users"]], ["kof311", "carol"])
        uid = body["users"][1]["id"]
        cookie = self.call("POST", "/api/auth/login", {"username": "carol", "password": "password123"})[2]["Set-Cookie"]
        cookie = cookie.split(";")[0].split("=", 1)[1]
        self.call("POST", "/api/profile", {"profile": PROFILE}, cookie=cookie)
        # restrict: logged out at once, can't log back in
        self.call("POST", "/api/users/status", {"id": uid, "status": "disabled"}, cookie=self.admin)
        self.assertEqual(self.call("GET", "/api/state", cookie=cookie)[0], 401)
        status, body, _ = self.call("POST", "/api/auth/login", {"username": "carol", "password": "password123"})
        self.assertEqual((status, body["error"]), (403, "This account has been restricted by the administrator."))
        self.call("POST", "/api/users/status", {"id": uid, "status": "active"}, cookie=self.admin)
        self.assertEqual(self.call("POST", "/api/auth/login", {"username": "carol", "password": "password123"})[0], 200)
        # the administrator can't be restricted or deleted
        self.assertEqual(self.call("POST", "/api/users/status", {"id": 1, "status": "disabled"}, cookie=self.admin)[0], 400)
        self.assertEqual(self.call("POST", "/api/users/delete", {"id": 1}, cookie=self.admin)[0], 400)
        # delete: the account and its data go
        status, body, _ = self.call("POST", "/api/users/delete", {"id": uid}, cookie=self.admin)
        self.assertEqual([u["username"] for u in body["users"]], ["kof311"])
        self.assertEqual(self.state.for_user(uid).get_profile(), {})
        self.assertIsNone(self.state.user_by_name("carol"))

    def test_reset_password(self):
        self.signup("dan", "password123")
        uid = self.state.user_by_name("dan")["id"]
        self.call("POST", "/api/users/password", {"id": uid, "password": "brandnew99"}, cookie=self.admin)
        self.assertEqual(self.call("POST", "/api/auth/login", {"username": "dan", "password": "password123"})[0], 401)
        self.assertEqual(self.call("POST", "/api/auth/login", {"username": "dan", "password": "brandnew99"})[0], 200)


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send_message(self, chat, text, **kw):
        self.sent.append((str(chat), text))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        (self.dir / "config.toml").write_text("", "utf-8")
        cfg = Config(telegram_bot_token="123:abc", telegram_chat_id="1000", apply_mode="auto",
                     apply_delay_seconds=[0, 0], message_language="en", telegram_api_id="1", telegram_api_hash="h")
        self.svc = Service(cfg, config_path=str(self.dir / "config.toml"))
        self.svc.transport = FakeTransport()
        self.st = self.svc.state
        self.uid = self.st.user_create("mei", auth.hash_password("password123"))

    def test_link_chat_with_start_code(self):
        self.st.user_update(self.uid, link_code="ABCD2345")
        self.assertIn("Linked", self.svc._link_chat({"id": 555, "type": "private"}, "abcd2345"))
        u = self.st.user_get(self.uid)
        self.assertEqual((u["bot_chat_id"], u["link_code"]), ("555", None))
        self.assertIn("wrong or expired", self.svc._link_chat({"id": 556}, "ABCD2345"))   # one-time
        self.assertEqual(self.svc.user_chats(), ["555"])
        self.st.user_update(self.uid, status="disabled")
        self.assertEqual(self.svc.user_chats(), [])                                       # restricted: no alerts

    def test_jobs_go_to_every_linked_user(self):
        from jobradar.models import Posting

        self.st.user_update(self.uid, bot_chat_id="555")
        p = Posting(title="Go dev", description="We're hiring", source="tg", source_tier="A",
                    source_url="https://t.me/x/1", posted_at=utcnow(), collected_at=utcnow(), content_hash="fp9")
        self.svc.pipeline.transport = self.svc.transport
        self.svc.pipeline.deliver_one(p)
        self.assertEqual(sorted(c for c, _ in self.svc.transport.sent), ["1000", "555"])

    def test_auto_apply_per_user(self):
        from jobradar.models import Posting

        self.st.user_update(self.uid, bot_chat_id="555")
        self.st.for_user(self.uid).set_profile(PROFILE)
        sess = acc.session_file(acc.session_path(self.svc.config, self.svc.base_dir, self.st.user_get(self.uid)))
        sess.parent.mkdir(parents=True, exist_ok=True)
        sess.write_text("x")
        sent = []

        class Sender:
            channel = "telegram"

            def __init__(self, path):
                self.path = path

            def send(self, contact, *, message, subject, attachment):
                sent.append((self.path, contact, attachment.name if attachment else None))
                return SendResult(peer_id="1", display_name="HR", text_id="7", file_id="8")

            def close(self):
                pass

        def senders(config, base):
            return {"telegram": Sender(config.telegram_session_path)}

        p = Posting(title="Go dev", description="Contact @hr_anna to apply", source="tg", source_tier="A",
                    source_url="https://t.me/x/1", posted_at=utcnow(), collected_at=utcnow(),
                    content_hash="fp1", source_type="telegram")
        from jobradar.resume import render
        with mock.patch("jobradar.apply.engine.build_senders", senders), \
                mock.patch("jobradar.apply.engine.template_draft",
                           lambda *a, **k: ApplicationDraft(None, "t", "Hello", "Application", "Hello")), \
                mock.patch.object(render, "pick_font", lambda *a, **k: None):
            cfg = self.svc.config.__class__(**{**self.svc.config.__dict__, "apply_keywords": ["go"]})
            self.svc.config = cfg
            report = EveryoneApplier(self.svc).run([p], utcnow())
        # only the user who has a Telegram login and a profile applied (the admin has neither here)
        self.assertEqual([(Path(s).name, c, f) for s, c, f in sent], [(sess.name, "@hr_anna", "Mei_Chen_Resume.pdf")])
        self.assertEqual(self.st.for_user(self.uid).get_application("fp1")["status"], "sent")
        self.assertIsNone(self.st.get_application("fp1"))                     # nothing under the admin
        self.assertTrue(any(c == "555" and "Applied" in t for c, t in self.svc.transport.sent))
        self.assertGreaterEqual(report.sent, 1)


if __name__ == "__main__":
    unittest.main()
