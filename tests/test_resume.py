"""Résumés generated from your profile: profile rules, the no-invention checks
on the AI's version, the PDF writer (embedded Chinese font), and auto-apply
attaching a résumé tailored to each job."""

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobradar import ai
from jobradar.ai import ApplicationDraft
from jobradar.apply.engine import Applier, build_applier
from jobradar.apply.senders import SendResult
from jobradar.config import Config
from jobradar.models import Posting
from jobradar.resume import builder as rb
from jobradar.resume import content as cv
from jobradar.resume import fonts, render
from jobradar.resume import profile as prof
from jobradar.resume.pdf import PdfDoc
from jobradar.state import State, utcnow

NOW = utcnow()

PROFILE = {
    "name": "陈美", "gender": "female", "birth_date": "1995-03", "location": "上海", "phone": "+86 138 0000 0000",
    "email": "mei@example.com", "job_title": "后端工程师",
    "summary": "五年后端开发经验，专注支付系统。",
    "skills": "后端：Go, Python\n数据库：PostgreSQL",
    "experience": [
        {"company": "蚂蚁科技", "role": "高级后端工程师", "location": "杭州", "start": "2021.07", "end": "",
         "details": "- 负责支付清结算系统，日均处理订单 300 万笔\n- 将对账服务从 Python 重写为 Go"},
        {"company": "小红科技", "role": "后端工程师", "start": "2019.07", "end": "2021.06",
         "details": "开发订单服务"},
    ],
    "projects": [{"name": "go-ledger", "role": "作者", "details": "复式记账库"},
                 {"name": "支付网关", "details": "统一支付接口"}],
    "education": [{"school": "复旦大学", "degree": "本科", "major": "计算机科学", "start": "2015.09", "end": "2019.06"}],
    "languages": [{"language": "英语", "level": "流利"}],
}

HAS_CJK_FONT = fonts.find_font("陈美后端工程师") is not None


def pdf_text(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        return subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True, check=True).stdout


def check_xref(test: unittest.TestCase, data: bytes) -> None:
    """Every xref offset points at its object; startxref points at the table."""
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", data).group(1))
    test.assertTrue(data[start:].startswith(b"xref\n"))
    rows = data[start:].split(b"\n")
    count = int(rows[1].split()[1])
    for i in range(1, count):
        off = int(rows[2 + i].split()[0])
        test.assertTrue(data[off:].startswith(f"{i} 0 obj".encode()), i)


class ProfileTests(unittest.TestCase):
    def test_required_fields(self):
        with self.assertRaises(prof.ProfileError) as e:
            prof.validate({"name": "陈美"})
        self.assertIn("Job title", str(e.exception))
        self.assertIn("Education", str(e.exception))
        self.assertTrue(prof.is_complete({"name": "Mei", "job_title": "Dev", "education": [{"school": "MIT"}]}))
        with self.assertRaises(prof.ProfileError):
            prof.validate({**PROFILE, "email": "not an email"})

    def test_normalize(self):
        p = prof.normalize({"name": " Mei ", "gender": "女", "experience": [{"company": ""}, {"company": "Acme"}],
                            "languages": "oops"})
        self.assertEqual((p["name"], p["gender"], len(p["experience"]), p["languages"]), ("Mei", "female", 1, []))

    def test_lines_and_skills(self):
        self.assertEqual(prof.lines("- a\n• b\n1. c\n\n（2）d"), ["a", "b", "c", "d"])
        self.assertEqual(prof.skill_groups("后端：Go, Python、gRPC\nDocker"),
                         [("后端", ["Go", "Python", "gRPC"]), ("", ["Docker"])])

    def test_ai_never_sees_contact_details(self):
        facts = json.dumps(ai.resume_facts(prof.normalize(PROFILE)), ensure_ascii=False)
        for secret in ("138 0000", "mei@example.com", "1995-03", "female"):
            self.assertNotIn(secret, facts)
        self.assertNotIn("138 0000", prof.facts_text(prof.normalize(PROFILE)))


class ContentTests(unittest.TestCase):
    def test_from_profile_order_and_labels(self):
        c = cv.from_profile(PROFILE, "zh")
        self.assertEqual(c.contact, ["女", "出生：1995-03", "上海", "+86 138 0000 0000", "mei@example.com"])
        self.assertEqual(c.experience[0].period, "2021.07 – 至今")
        self.assertEqual(c.experience[0].bullets, ["负责支付清结算系统，日均处理订单 300 万笔", "将对账服务从 Python 重写为 Go"])
        self.assertEqual(c.skills[0], ("后端", ["Go", "Python"]))
        self.assertEqual(cv.from_profile(PROFILE, "en").contact[:2], ["Female", "Born 1995-03"])
        self.assertEqual(cv.file_stem(PROFILE, "zh"), "陈美_简历")
        self.assertEqual(cv.file_stem({"name": "Mei Chen"}, "en"), "Mei_Chen_Resume")

    def test_sections_left_out_when_empty(self):
        c = cv.from_profile({"name": "Mei", "job_title": "Dev", "education": [{"school": "MIT"}]}, "en")
        self.assertEqual((c.intro, c.skills, c.experience, c.projects, c.languages), ("", [], [], [], []))

    def ai_answer(self, **over):
        obj = {
            "job_title": "Backend Engineer",
            "intro": "Backend engineer focused on payment systems.",
            "tech_stack": [{"category": "Backend", "items": [
                {"text": "Go", "source": "Go"},
                {"text": "Rust", "source": "Rust"},                      # not in the profile
                {"text": "PostgreSQL", "source": "PostgreSQL"}]}],
            "experience": [
                {"index": 0, "company": "Ant Tech", "role": "Senior Backend Engineer", "location": "Hangzhou",
                 "bullets": [
                     {"text": "Built the settlement system for 3M orders a day", "source": "日均处理订单 300 万笔"},
                     {"text": "Rewrote reconciliation from Python to Go", "source": "将对账服务从 Python 重写为 Go"}]},
                {"index": 1, "company": "Xiaohong", "role": "Backend Engineer", "location": "",
                 "bullets": [{"text": "Led a team of 12", "source": "开发订单服务"}]},   # invented number
                {"index": 7, "company": "Google", "role": "Staff", "location": "", "bullets": []},  # invented job
            ],
            "project_order": [1, 0],
            "projects": [{"index": 1, "name": "Payment gateway", "role": "",
                          "bullets": [{"text": "Unified payment API", "source": "统一支付接口"}]}],
            "education": [{"index": 0, "school": "Fudan University", "degree": "BSc", "major": "CS", "bullets": []}],
            "languages": [{"index": 0, "text": "English — fluent"}],
        }
        obj.update(over)
        return obj

    def test_ai_version_is_checked_against_the_profile(self):
        c = cv.from_ai(self.ai_answer(), PROFILE, "en")
        self.assertTrue(c.tailored)
        self.assertEqual(c.skills, [("Backend", ["Go", "PostgreSQL"])])          # Rust dropped
        self.assertEqual([e.title for e in c.experience], ["Ant Tech", "Xiaohong"])  # no Google
        self.assertEqual(c.experience[0].period, "2021.07 – Present")          # dates from the profile
        self.assertEqual(c.experience[0].bullets[0], "Built the settlement system for 3M orders a day")
        # "3M" has a 3 that's in the source (300万); a bullet adding "12" is dropped → your own line
        self.assertEqual(c.experience[1].bullets, ["开发订单服务"])
        self.assertEqual([p.title for p in c.projects], ["Payment gateway", "go-ledger"])   # most relevant first
        self.assertEqual(c.projects[1].bullets, ["复式记账库"])
        self.assertEqual(c.languages, ["English — fluent"])
        self.assertEqual(c.contact[0], "Female")

    def test_numbers_must_come_from_the_profile(self):
        c = cv.from_ai(self.ai_answer(intro="10 years building payment systems."), PROFILE, "en")
        self.assertEqual(c.intro, PROFILE["summary"])        # "10" isn't in the profile → your summary
        c = cv.from_ai(self.ai_answer(tech_stack=[]), PROFILE, "en")
        self.assertEqual(c.skills, cv.from_profile(PROFILE, "en").skills)

    def test_garbage_falls_back_to_the_profile(self):
        self.assertFalse(cv.from_ai(None, PROFILE, "zh").tailored)
        c = cv.from_ai({"experience": "nope", "tech_stack": [1, None], "languages": [{"index": "x"}]}, PROFILE, "zh")
        self.assertEqual(c.experience[0].bullets, cv.from_profile(PROFILE, "zh").experience[0].bullets)
        self.assertEqual(c.languages, ["英语 · 流利"])


class PdfTests(unittest.TestCase):
    def test_helvetica_fallback_is_a_valid_pdf(self):
        c = cv.from_profile({"name": "Mei Chen", "job_title": "Backend Engineer", "education": [{"school": "MIT"}],
                             "experience": [{"company": "Acme", "details": "- Built things “fast” – really"}]}, "en")
        doc = PdfDoc(None, title="t")
        self.assertGreater(doc.text_width("Hello", 10), 0)
        with mock.patch.object(render, "pick_font", lambda *a, **k: None):
            data = render.render(c)
        self.assertTrue(data.startswith(b"%PDF-1.7") and data.endswith(b"%%EOF\n"))
        self.assertIn(b"/Helvetica-Bold", data)
        check_xref(self, data)

    def test_no_chinese_font_is_a_clear_error(self):
        with mock.patch.object(render, "find_font", lambda *a, **k: None):
            with self.assertRaises(render.ResumeError) as e:
                render.render(cv.from_profile(PROFILE, "zh"))
        self.assertIn("fonts folder", str(e.exception))

    def test_wrapping(self):
        doc = PdfDoc(None)
        lines = doc.wrap("one two three four five six seven eight nine ten", 10, 60)
        self.assertGreater(len(lines), 2)
        self.assertTrue(all(doc.text_width(x, 10) <= 60 for x in lines))
        self.assertEqual(" ".join(lines), "one two three four five six seven eight nine ten")
        self.assertEqual(doc.wrap("a\nb", 10, 100), ["a", "b"])

    @unittest.skipUnless(HAS_CJK_FONT, "no Chinese TrueType font installed")
    def test_chinese_resume_embeds_a_small_font(self):
        font = fonts.find_font("陈美后端工程师")
        data = render.render(cv.from_profile(PROFILE, "zh"), font=font)
        check_xref(self, data)
        self.assertIn(b"/FontFile2", data)
        self.assertIn(b"/ToUnicode", data)
        self.assertLess(len(data), 400_000)
        self.assertLess(len(data), len(font.data) / 10)       # subset, not the whole font
        if shutil.which("pdftotext"):
            text = pdf_text(data)
            for s in ("陈美", "后端工程师", "个人简介", "技术栈", "工作经历", "项目经历", "教育背景", "语言能力",
                      "蚂蚁科技", "复旦大学", "2021.07 – 至今"):
                self.assertIn(s, text)
            order = [text.index(s) for s in ("个人简介", "技术栈", "工作经历", "项目经历", "教育背景", "语言能力")]
            self.assertEqual(order, sorted(order))

    @unittest.skipUnless(HAS_CJK_FONT, "no Chinese TrueType font installed")
    def test_subset_is_a_readable_font(self):
        font = fonts.find_font("陈美")
        gids = {font.gid("陈"), font.gid("美")}
        sub = fonts.TrueTypeFont(font.subset(gids, {g: "x" for g in []}), 0, "subset")
        self.assertEqual(sub.num_glyphs, font.num_glyphs)
        for g in gids:
            s, e = sub._glyph_range(g)
            self.assertGreater(e - s, 0)
        s, e = sub._glyph_range(font.gid("的"))
        self.assertEqual(e - s, 0)                               # unused glyph emptied
        sub2 = fonts.TrueTypeFont(font.subset(gids, {font.gid("陈"): "陈"}), 0, "subset")
        self.assertEqual(sub2.gid("陈"), font.gid("陈"))           # the small cmap works

    def test_long_resume_breaks_pages(self):
        p = {**PROFILE, "experience": [{"company": f"公司{i}", "details": "\n".join(["负责很多事情"] * 6)}
                                       for i in range(15)]}
        with mock.patch.object(render, "pick_font", lambda *a, **k: None):
            doc_pages = []
            orig = PdfDoc.to_bytes

            def spy(self):
                doc_pages.append(len(self.pages))
                return orig(self)
            with mock.patch.object(PdfDoc, "to_bytes", spy):
                try:
                    render.render(cv.from_profile(p, "en"))
                except UnicodeEncodeError:
                    pass
        self.assertGreater(doc_pages[0], 1)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.latin = {"name": "Mei Chen", "job_title": "Backend Engineer", "education": [{"school": "MIT"}],
                      "skills": "Go, Python"}

    def test_build_writes_a_named_file(self):
        b = rb.ResumeBuilder(Config(message_language="en"), self.dir, self.latin)
        with mock.patch.object(render, "pick_font", lambda *a, **k: None):
            path = b.build("We need a Go developer", key="abc/def")
        self.assertEqual(path, self.dir / "resumes" / "abcdef" / "Mei_Chen_Resume.pdf")
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        self.assertEqual(b.relative(path), "resumes/abcdef/Mei_Chen_Resume.pdf")

    def test_tailor_failure_uses_your_wording(self):
        def boom(*a):
            raise RuntimeError("down")
        b = rb.ResumeBuilder(Config(message_language="en"), self.dir, self.latin, tailor=boom)
        self.assertFalse(b.content("post").tailored)

    def test_incomplete_profile_gives_no_builder(self):
        self.assertIsNone(rb.make_builder(Config(), self.dir, {"name": "Mei"}))
        self.assertIsNotNone(rb.make_builder(Config(), self.dir, self.latin))

    def test_ai_tailor_call(self):
        seen = {}

        def fake(url, body, headers, timeout):
            seen.update(body)
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"intro": "x"})}]}

        with mock.patch.object(ai, "_post_json", fake):
            out = ai.tailor_resume(prof.normalize(PROFILE), "Hiring Go devs", language="zh", api_key="k")
        self.assertEqual(out, {"intro": "x"})
        self.assertIn("简体中文", seen["system"])
        self.assertIn("Never add employers", seen["system"])
        self.assertEqual(seen["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("138 0000", json.dumps(seen["messages"], ensure_ascii=False))


class FakeSender:
    channel = "telegram"

    def __init__(self):
        self.sent = []

    def send(self, contact, *, message, subject, attachment):
        self.sent.append((contact, attachment))
        return SendResult(peer_id="1", display_name="HR", text_id="10", file_id="11")

    def close(self):
        pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = State(self.dir / "jobradar.db")
        self.notes = []
        self.sender = FakeSender()
        self.profile = {"name": "Mei Chen", "job_title": "Backend Engineer", "education": [{"school": "MIT"}]}

    def posting(self, key="fp1", contact="@hr_anna"):
        return Posting(title="Go Developer", description=f"We're hiring a Go developer. Contact {contact}",
                       source="tg", source_tier="A", source_url="https://t.me/jobs/1", posted_at=NOW,
                       collected_at=NOW, content_hash=key, source_type="telegram")

    def applier(self, mode="auto", builder=None, resume_pdf=None):
        cfg = Config(apply_mode=mode, apply_delay_seconds=[0, 0], message_language="en")
        return Applier(cfg, self.state, senders={"telegram": self.sender},
                       drafter=lambda e: ApplicationDraft(85, "good", "Hello", "Application", "Hello"),
                       notify=self.notes.append, resume_pdf=resume_pdf, resume_builder=builder,
                       ai_available=True, sleep=lambda s: None)

    def test_each_application_gets_its_own_resume(self):
        b = rb.ResumeBuilder(Config(message_language="en"), self.dir, self.profile)
        with mock.patch.object(render, "pick_font", lambda *a, **k: None):
            self.applier(builder=b).run([self.posting("fp1"), self.posting("fp2", "@hr_bob")], NOW)
        files = [a for _, a in self.sender.sent]
        self.assertEqual([f.name for f in files], ["Mei_Chen_Resume.pdf"] * 2)
        self.assertNotEqual(files[0].parent, files[1].parent)            # one per posting
        self.assertEqual(self.state.get_application("fp1")["resume_file"], "resumes/fp1/Mei_Chen_Resume.pdf")
        conv = self.state.conversations()[0]
        atts = [m["attachment"] for m in self.state.conversation(conv["id"])["messages"]]
        self.assertIn("Mei_Chen_Resume.pdf", atts)

    def test_dry_run_shows_the_tailored_resume(self):
        b = rb.ResumeBuilder(Config(message_language="en"), self.dir, self.profile)
        with mock.patch.object(render, "pick_font", lambda *a, **k: None):
            self.applier("dry_run", builder=b).run([self.posting()], NOW)
        self.assertEqual(self.sender.sent, [])
        self.assertIn("Résumé tailored for this job: Mei_Chen_Resume.pdf", self.notes[0])
        self.assertTrue((self.dir / self.state.get_application("fp1")["resume_file"]).exists())

    def test_failed_resume_falls_back_to_the_uploaded_pdf(self):
        up = self.dir / "resume.pdf"
        up.write_bytes(b"%PDF-1.4")
        b = mock.Mock(build=mock.Mock(side_effect=render.ResumeError("no font")))
        self.applier(builder=b, resume_pdf=up).run([self.posting()], NOW)
        self.assertEqual(self.sender.sent[0][1], up)
        self.assertIn("no font", self.notes[0])

    def test_no_resume_means_nothing_is_sent(self):
        b = mock.Mock(build=mock.Mock(side_effect=render.ResumeError("no font")))
        for _ in range(3):
            self.applier(builder=b).run([self.posting()], NOW)
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.state.get_application("fp1")["status"], "failed")

    def test_build_applier_uses_the_profile(self):
        self.state.set_profile(self.profile)
        cfg = Config(apply_mode="dry_run", apply_keywords=["go"], message_language="en")
        a = build_applier(cfg, self.state, base_dir=self.dir)
        self.assertIsNotNone(a.resume_builder)
        self.assertIn("Mei Chen", a.drafter({"title": "Go", "description": "Go dev"}).message)
        a2 = build_applier(Config(resume_source="uploaded"), self.state, base_dir=self.dir)
        self.assertIsNone(a2.resume_builder)


class DashboardTests(unittest.TestCase):
    def test_profile_save_preview_and_file(self):
        import threading
        import urllib.error
        import urllib.request

        from jobradar.web import app as web

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text('message_language = "en"\n', "utf-8")
            server = web.make_server(cfg, port=0)
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                h = {"Cookie": f"{web.COOKIE}={server.token}", "X-Requested-With": "jobradar",
                     "Content-Type": "application/json"}

                def call(method, path, body=None, raw=False):
                    r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                               method=method, headers=h)
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        return resp.read() if raw else json.loads(resp.read())

                def error(method, path, body=None):
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        call(method, path, body)
                    return json.loads(e.exception.read())["error"]

                self.assertFalse(call("GET", "/api/profile")["complete"])
                self.assertIn("Job title", error("POST", "/api/profile", {"profile": {"name": "Mei"}}))
                p = {"name": "Mei Chen", "job_title": "Backend Engineer", "education": [{"school": "MIT"}],
                     "phone": "+60 12", "skills": "Go, Python"}
                saved = call("POST", "/api/profile", {"profile": p})
                self.assertTrue(saved["apply"]["profile_complete"])
                self.assertEqual(call("GET", "/api/profile")["profile"]["phone"], "+60 12")
                with mock.patch.object(render, "pick_font", lambda *a, **k: None):
                    prev = call("POST", "/api/profile/preview", {})
                self.assertEqual((prev["name"], prev["tailored"]), ("Mei_Chen_Resume.pdf", False))
                pdf = call("GET", "/api/file?path=" + prev["file"], raw=True)
                self.assertTrue(pdf.startswith(b"%PDF"))
                self.assertIn("not found", error("GET", "/api/file?path=../config.toml"))
                self.assertIn("not found", error("GET", "/api/file?path=jobradar.db"))
                self.assertIn("AI key", error("POST", "/api/profile/import", {}))
            finally:
                server.shutdown()
                server.server_close()


class StateTests(unittest.TestCase):
    def test_profile_roundtrip_and_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "jobradar.db"
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE applications (fingerprint TEXT PRIMARY KEY, at TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, channel TEXT, contact TEXT, title TEXT NOT NULL DEFAULT '',
                source_url TEXT, score INTEGER, reason TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '');
                INSERT INTO applications (fingerprint, at, mode, status) VALUES ('old', '2026-01-01', 'auto', 'sent');
                PRAGMA user_version=5;""")
            db.close()
            st = State(path)
            self.assertEqual(st.get_application("old")["resume_file"], "")
            self.assertEqual(st.get_profile(), {})
            st.set_profile({"name": "陈美"})
            st.set_profile(PROFILE)
            self.assertEqual(st.get_profile()["experience"][0]["company"], "蚂蚁科技")
            st.close()
            self.assertEqual(sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
