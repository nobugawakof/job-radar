"""One data folder (jobradar-data/) and one-file backup / restore."""

import io
import json
import os
import socket
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from threading import Thread
from unittest import mock

from jobradar import cli, datadir
from jobradar.backup import BackupError, make_backup, read_backup, restore_backup
from jobradar.state import State, iso, utcnow
from jobradar.web import app as web


class TmpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)


class DataFolderTests(TmpCase):
    def test_fresh_install_uses_the_folder(self):
        self.assertEqual(datadir.default_config_path(self.base), self.base / "jobradar-data" / "config.toml")

    def test_old_layout_is_moved(self):
        (self.base / "config.toml").write_text('state_path = "my-state.db"\nresume_pdf_path = "cv.pdf"\n', "utf-8")
        for name in ("my-state.db", "jobradar-user.session", "cv.pdf", "jobradar-web.token", "notes.txt"):
            (self.base / name).write_text("x", "utf-8")
        cfg = datadir.default_config_path(self.base)
        data = self.base / "jobradar-data"
        self.assertEqual(cfg, data / "config.toml")
        self.assertEqual(sorted(p.name for p in data.iterdir()),
                         ["config.toml", "cv.pdf", "jobradar-user.session", "jobradar-web.token", "my-state.db"])
        self.assertTrue((self.base / "notes.txt").exists())  # not ours: left alone

    def test_clash_means_no_move(self):
        (self.base / "config.toml").write_text("", "utf-8")
        (self.base / "jobradar.db").write_text("old", "utf-8")
        (self.base / "jobradar-data").mkdir()
        (self.base / "jobradar-data" / "jobradar.db").write_text("new", "utf-8")
        self.assertFalse(datadir.migrate_loose_files(self.base))
        self.assertEqual((self.base / "jobradar.db").read_text(), "old")

    def test_failed_move_puts_everything_back(self):
        (self.base / "config.toml").write_text("", "utf-8")
        for name in ("jobradar.db", "jobradar-user.session", "resume.pdf"):
            (self.base / name).write_text(name, "utf-8")
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 3:
                raise PermissionError("file in use")
            return real(src, dst)

        with mock.patch("jobradar.datadir.os.replace", flaky):
            self.assertFalse(datadir.migrate_loose_files(self.base))
        for name in ("config.toml", "jobradar.db", "jobradar-user.session", "resume.pdf"):
            self.assertTrue((self.base / name).exists(), name)

    def test_not_moved_while_another_job_radar_runs(self):
        (self.base / "config.toml").write_text("", "utf-8")
        st = State(self.base / "jobradar.db")
        with st._tx() as db:  # a live process on this machine holds the run lock
            db.execute("INSERT OR REPLACE INTO meta VALUES ('run_lock', ?)", (json.dumps(
                {"owner": "other", "pid": os.getppid(), "host": socket.gethostname(), "at": iso(utcnow())}),))
        st.close()
        self.assertFalse(datadir.migrate_loose_files(self.base))

    def test_explicit_config_is_left_alone(self):
        cfg = self.base / "config.toml"
        cfg.write_text("", "utf-8")
        with mock.patch.object(cli, "base_dir", return_value=self.base):
            cli.main(["--config", str(cfg), "sources"])
        self.assertFalse((self.base / "jobradar-data").exists())


def installation(base: Path, *, absolute_paths=False):
    data = base / "jobradar-data"
    data.mkdir(parents=True, exist_ok=True)
    prefix = f"{data}/" if absolute_paths else ""
    (data / "config.toml").write_text(
        f'keywords = ["python"]\nstate_path = "{prefix}jobradar.db"\n'
        f'telegram_session_path = "{prefix}jobradar-user.session"\n', "utf-8")
    st = State(data / "jobradar.db")
    st.mark_sent("fpA")
    sqlite3.connect(data / "jobradar-user.session").execute("CREATE TABLE sessions (x)").connection.commit()
    (data / "resume.pdf").write_bytes(b"%PDF-1.4 me")
    return data, st


class BackupTests(TmpCase):
    def test_login_only_when_asked(self):
        data, st = installation(self.base)
        names = lambda b: sorted(zipfile.ZipFile(io.BytesIO(b)).namelist())
        self.assertNotIn("jobradar-user.session", names(make_backup(data / "config.toml", state=st)))
        self.assertIn("jobradar-user.session", names(make_backup(data / "config.toml", include_login=True)))

    def test_snapshot_of_the_open_database(self):
        data, st = installation(self.base)
        st.mark_sent("fpB")  # committed through the live connection, possibly still in the WAL
        manifest, contents = read_backup(make_backup(data / "config.toml", state=st))
        copy = self.base / "copy.db"
        copy.write_bytes(contents["database"])
        self.assertTrue(State(copy).already_sent("fpB"))

    def test_bad_backups_are_refused(self):
        good = zipfile.ZipFile(io.BytesIO(make_backup(installation(self.base)[0] / "config.toml")))

        def zipped(files):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for n, d in files.items():
                    z.writestr(n, d)
            return buf.getvalue()

        base_files = {n: good.read(n) for n in good.namelist()}
        cases = [
            b"not a zip",
            zipped({"config.toml": b""}),                                        # no manifest
            zipped({**base_files, "../evil.py": b"x"}),                           # unexpected path
            zipped({**base_files, "jobradar.db": b"not sqlite"}),
            zipped({**base_files, "config.toml": b"= broken toml"}),
            zipped({**base_files, "resume.pdf": b"MZ executable"}),
            zipped({**base_files, "jobradar-backup.json": b'{"app": "something-else"}'}),
        ]
        for i, data in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(BackupError):
                read_backup(data)


class RestoreTests(TmpCase):
    def test_restore_from_another_computer(self):
        src, st = installation(self.base / "old-pc", absolute_paths=True)
        with st._tx() as db:  # snapshot taken mid-check
            db.execute("INSERT OR REPLACE INTO meta VALUES ('run_lock', ?)", (json.dumps(
                {"owner": "x", "pid": os.getpid(), "host": socket.gethostname(), "at": iso(utcnow())}),))
        backup = make_backup(src / "config.toml", include_login=True, state=st)

        dst = self.base / "new-pc" / "jobradar-data"
        dst.mkdir(parents=True)
        (dst / "config.toml").write_text('keywords = ["old settings"]\n', "utf-8")
        result = restore_backup(backup, dst / "config.toml")

        cfg = (dst / "config.toml").read_text("utf-8")
        self.assertIn('state_path = "jobradar.db"', cfg)                 # paths normalised
        self.assertIn('keywords = ["python"]', cfg)
        restored = State(dst / "jobradar.db")
        self.assertTrue(restored.already_sent("fpA"))
        self.assertTrue(restored.acquire_run_lock("me"))                 # stale lock cleared
        self.assertTrue((dst / "jobradar-user.session").exists())
        prev = Path(result["previous_files"])
        self.assertEqual((prev / "config.toml").read_text("utf-8"), 'keywords = ["old settings"]\n')


class DashboardTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.data, st = installation(self.base)
        st.close()
        self.server = web.make_server(self.data / "config.toml", port=0)
        Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.headers = {"Cookie": f"{web.COOKIE}={self.server.token}", "X-Requested-With": "jobradar"}

    def req(self, method, path, data=None):
        r = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_download_and_restore(self):
        status, headers, backup = self.req("GET", "/api/backup?login=0")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertNotIn("jobradar-user.session", zipfile.ZipFile(io.BytesIO(backup)).namelist())

        self.server.ctrl.service.state.mark_sent("after-backup")  # change, then restore
        status, _, body = self.req("PUT", "/api/restore", backup)
        self.assertEqual(status, 200, body)
        st = self.server.ctrl.service.state                         # a fresh, working connection
        self.assertTrue(st.already_sent("fpA"))
        self.assertFalse(st.already_sent("after-backup"))
        self.assertEqual(json.loads(body)["sent_total"], 1)

    def test_restore_refused_during_a_check(self):
        _, _, backup = self.req("GET", "/api/backup")
        self.server.ctrl.running = True
        self.assertEqual(self.req("PUT", "/api/restore", backup)[0], 409)

    def test_bad_file_changes_nothing(self):
        status, _, body = self.req("PUT", "/api/restore", b"not a zip")
        self.assertEqual(status, 400)
        self.assertTrue(self.server.ctrl.service.state.already_sent("fpA"))


class CliTests(TmpCase):
    def test_backup_and_restore_commands(self):
        data, st = installation(self.base)
        st.close()
        cfg = str(data / "config.toml")
        out = self.base / "b.zip"
        self.assertEqual(cli.main(["--config", cfg, "backup", "-o", str(out)]), 0)
        State(data / "jobradar.db").mark_sent("later")
        self.assertEqual(cli.main(["--config", cfg, "restore", str(out), "--yes"]), 0)
        self.assertFalse(State(data / "jobradar.db").already_sent("later"))


if __name__ == "__main__":
    unittest.main()
