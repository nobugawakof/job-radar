"""Dashboard: config writer, HTTP security, API, and the background runner."""

import json
import logging
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from jobradar.pipeline import RunSummary
from jobradar.redact import redact
from jobradar.state import utcnow
from jobradar.web import app as web
from jobradar.web import configfile as cf

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"

    def test_example_round_trips_and_keeps_sources_and_unknown_keys(self):
        raw = cf.read_raw(EXAMPLE)
        raw["sources"] = [{"name": "feed", "type": "rss", "url": "https://e.com/jobs.rss"}]
        raw["some future key"] = "a\x7fb \"q\" \\ 中文\n"
        cf.write(self.path, raw)
        self.assertEqual(tomllib.loads(self.path.read_text("utf-8")), raw)

    def test_backup_made_once(self):
        self.path.write_text('keywords = ["a"] # my comment\n', "utf-8")
        cf.write(self.path, cf.apply_changes(cf.read_raw(self.path), {"keywords": ["b"]}))
        cf.write(self.path, cf.apply_changes(cf.read_raw(self.path), {"keywords": ["c"]}))
        self.assertIn("my comment", self.path.with_name("config.toml.bak").read_text("utf-8"))
        self.assertEqual(cf.read_raw(self.path)["keywords"], ["c"])

    def test_refuses_to_write_if_it_would_not_round_trip(self):
        self.path.write_text('keywords = ["a"]\n', "utf-8")
        with mock.patch.object(cf, "render", return_value='keywords = ["WRONG"]\n'):
            with self.assertRaises(cf.ConfigError):
                cf.write(self.path, {"keywords": ["a"]})
        self.assertEqual(cf.read_raw(self.path), {"keywords": ["a"]})

    def test_validation(self):
        bad = {"apply_mode": "maybe", "apply_daily_cap": -1, "remote_only": "yes",
               "telegram_api_id": "abc", "apply_channels": ["fax"], "unknown_key": 1,
               "run_interval_hours": "soon"}
        for k, v in bad.items():
            with self.subTest(k=k), self.assertRaises(cf.ConfigError):
                cf.coerce(k, v)
        out = cf.apply_changes({"gemini_api_key": "old"}, {
            "keywords": "python, backend\n后端", "gemini_api_key": None, "apply_delay_seconds": [30, 90]})
        self.assertEqual(out, {"keywords": ["python", "backend", "后端"], "apply_delay_seconds": [30.0, 90.0]})
        with self.assertRaises(cf.ConfigError):
            cf.apply_changes({}, {"apply_delay_seconds": [30]})


class RedactTests(unittest.TestCase):
    def test_tokens_are_redacted(self):
        s = redact("HTTP 401 for https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe "
                   "AIzaSyA1234567890abcdefghijklmnop sk-ant-api03-abcdefghijklmnop ?key=secret123")
        for secret in ("AAHdqTcv", "AIzaSyA123", "sk-ant-api03", "secret123"):
            self.assertNotIn(secret, s)

    def test_log_buffer_redacts(self):
        buf = web.LogBuffer()
        logger = logging.getLogger("jobradar.test-redact")
        logger.addHandler(buf)
        self.addCleanup(logger.removeHandler, buf)
        logger.warning("failed: https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/x")
        self.assertNotIn("AAHdqTcv", buf.since(0)[-1]["msg"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = self.dir / "config.toml"
        self.cfg.write_text('keywords = ["python"]\n', "utf-8")
        self.server = web.make_server(self.cfg, port=0)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = f"{web.COOKIE}={self.server.token}"

    def call(self, method, path, body=None, *, cookie=True, xhr=True, origin=None, host=None, raw=None):
        headers = {}
        if cookie:
            headers["Cookie"] = self.cookie
        if xhr:
            headers["X-Requested-With"] = "jobradar"
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def j(self, *a, **kw):
        status, _, body = self.call(*a, **kw)
        return status, json.loads(body)

    # ------------------------------------------------------------ security
    def test_locked_without_session(self):
        self.assertEqual(self.call("GET", "/", cookie=False)[0], 200)            # the login screen
        status, body = self.j("GET", "/api/state", cookie=False)
        self.assertEqual((status, body["login"]), (401, True))

    def test_token_link_sets_cookie_and_redirects(self):
        status, headers, _ = self.call("GET", f"/?t={self.server.token}", cookie=False)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        status, headers, _ = self.call("GET", "/?t=wrong", cookie=False)
        self.assertEqual(status, 303)
        self.assertNotIn("Set-Cookie", headers)                                   # a wrong link logs nobody in

    def test_page_is_served_with_csp(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Job Radar", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_dns_rebinding_host_refused(self):
        self.assertEqual(self.call("GET", "/api/state", host=f"evil.example:{self.port}")[0], 403)

    def test_csrf_protection(self):
        body = {"changes": {"keywords": ["x"]}}
        self.assertEqual(self.call("POST", "/api/config", body, xhr=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/config", body, origin="https://evil.example")[0], 403)
        self.assertEqual(self.call("POST", "/api/config", body, origin=self.base)[0], 200)

    def test_secrets_never_reach_the_browser(self):
        self.j("POST", "/api/config", {"changes": {"gemini_api_key": "AIzaSECRETSECRETSECRET9876"}})
        status, _, body = self.call("GET", "/api/state")
        self.assertNotIn(b"SECRETSECRET", body)
        state = json.loads(body)
        self.assertEqual(state["secrets"]["gemini_api_key"], {"set": True, "env": False, "hint": "…9876"})
        # An empty secret field means "keep the saved one".
        self.j("POST", "/api/config", {"changes": {"gemini_api_key": "", "keywords": ["go"]}})
        self.assertEqual(cf.read_raw(self.cfg)["gemini_api_key"], "AIzaSECRETSECRETSECRET9876")

    # ----------------------------------------------------------------- api
    def test_save_and_validation_errors(self):
        status, st = self.j("POST", "/api/config", {"changes": {"keywords": ["python", "后端"], "remote_only": False}})
        self.assertEqual((status, st["config"]["keywords"], st["config"]["remote_only"]), (200, ["python", "后端"], False))
        self.assertEqual(cf.read_raw(self.cfg)["keywords"], ["python", "后端"])
        status, err = self.j("POST", "/api/config", {"changes": {"apply_daily_cap": -5}})
        self.assertEqual(status, 400)
        self.assertIn("negative", err["error"])

    def test_auto_mode_requires_resume(self):
        status, err = self.j("POST", "/api/config", {"changes": {"apply_mode": "auto"}})
        self.assertEqual(status, 400)
        status, _ = self.j("PUT", "/api/resume", raw=b"plain text")
        self.assertEqual(status, 400)
        status, st = self.j("PUT", "/api/resume", raw=b"%PDF-1.4 fake")
        self.assertTrue(st["apply"]["resume_pdf"]["exists"])
        self.assertEqual(self.j("POST", "/api/config", {"changes": {"apply_mode": "auto"}})[0], 200)

    def test_placeholder_token_is_not_configured(self):
        self.cfg.write_text('telegram_bot_token = "PASTE-YOUR-BOT-TOKEN"\ntelegram_chat_id = "PASTE"\n', "utf-8")
        self.server.ctrl.reload()
        st = self.j("GET", "/api/state")[1]
        self.assertFalse(st["configured"])
        self.assertFalse(st["secrets"]["telegram_bot_token"]["set"])
        self.assertEqual(self.j("POST", "/api/run", {})[0], 400)

    def test_find_chats(self):
        updates = [{"message": {"chat": {"id": 42, "type": "private", "first_name": "Mei"}}},
                   {"message": {"chat": {"id": 42, "type": "private", "first_name": "Mei"}}},
                   {"my_chat_member": {"chat": {"id": -100, "type": "channel", "title": "Jobs"}}}]
        from jobradar.delivery import inbox as inbox_mod

        pending = [dict(u, update_id=i) for i, u in enumerate(updates, 1)]
        get_me = lambda token, method, params=None, timeout=15: {"username": "my_jobs_bot", "first_name": "Jobs"}

        def fake_call(self_, method, params=None, timeout=None):
            if method == "getUpdates":
                out = [u for u in pending if u["update_id"] >= int(params["offset"])]
                return out
            return True

        with mock.patch.object(web, "bot_call", get_me), mock.patch.object(inbox_mod.BotInbox, "call", fake_call):
            status, out = self.j("POST", "/api/telegram/find", {"token": "123456:ABCDEF"})
            self.assertEqual(out["bot"]["username"], "my_jobs_bot")
            self.assertEqual({c["id"] for c in out["chats"]}, {"42", "-100"})
            # The updates are now confirmed, but the chats are remembered.
            status, again = self.j("POST", "/api/telegram/find", {})
            self.assertEqual({c["id"] for c in again["chats"]}, {"42", "-100"})
        self.assertEqual(cf.read_raw(self.cfg)["telegram_bot_token"], "123456:ABCDEF")  # saved

    def test_manual_run_uses_background_runner(self):
        self.j("POST", "/api/config", {"changes": {"telegram_bot_token": "1:x", "telegram_chat_id": "42"}})
        ctrl = self.server.ctrl
        fake = RunSummary(trigger="manual", started_at=utcnow(), items_fetched=7, delivered=2)

        def fake_run(trigger):
            ctrl.service.state.mark_run(utcnow())
            return fake

        from jobradar.delivery import inbox as inbox_mod

        # the runner also starts the bot inbox; keep it off the network
        with mock.patch.object(ctrl.service, "_safe_run", side_effect=fake_run), \
                mock.patch.object(inbox_mod.BotInbox, "poll", return_value=0):
            ctrl.start()
            self.addCleanup(ctrl.stop)
            self.assertEqual(self.j("POST", "/api/run", {})[0], 200)
            for _ in range(50):
                if ctrl.last_summary:
                    break
                time.sleep(0.05)
        st = self.j("GET", "/api/state")[1]
        self.assertEqual((st["last_summary"]["fetched"], st["last_summary"]["delivered"]), (7, 2))

    def test_auto_run_schedule(self):
        ctrl = self.server.ctrl
        self.assertIsNone(ctrl.next_run())  # off by default: half-finished setups never send
        self.j("POST", "/api/config", {"changes": {"telegram_bot_token": "1:x", "telegram_chat_id": "42",
                                                   "auto_run": True}})
        self.assertLessEqual(ctrl.next_run(), web.iso(utcnow()))  # never ran → due now
        ctrl.service.state.mark_run(utcnow())
        self.assertGreater(ctrl.next_run(), web.iso(utcnow()))

    def test_settings_reload_keeps_one_shared_state(self):
        ctrl = self.server.ctrl
        before = ctrl.service.state
        self.j("POST", "/api/config", {"changes": {"keywords": ["rust"]}})
        self.assertIs(ctrl.service.state, before)
        self.assertEqual(ctrl.config.keywords, ["rust"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


if __name__ == "__main__":
    unittest.main()
