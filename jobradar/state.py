"""Persistent state, stored in a single SQLite database file (``jobradar.db``).

SQLite ships with Python, so this adds no dependency and no server: the whole
database is one file next to your config (SRS DR-1, DR-2). It remembers:

* **sent** — fingerprints of postings already delivered, so nothing is sent twice;
* **sources** — per-source last success, consecutive failures, disabled flag;
* **signatures** — near-duplicate signatures of sent postings (reworded reposts);
* **jobs** — every delivered job with its full original text (FR-12; history
  for the dashboard and the input for future résumé matching);
* **applications / apply_queue / contacts / apply_paused** — auto-apply records;
* **meta** — the last run time and the schema version.

Every change is committed in its own transaction, and the database runs in WAL
mode, so a crash or a closed laptop can never leave it half-written (NFR-4), and
the dashboard and a command-line run can use it at the same time. One
connection is shared by all threads behind a lock.

Upgrading from the old ``jobradar-state.json`` is automatic: on first open it is
imported once and renamed to ``*.json.migrated``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("jobradar.state")

SCHEMA_VERSION = 5   # 2: runs; 3: bot inbox; 4: review queue; 5: conversations + messages


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sent (
    fingerprint TEXT PRIMARY KEY,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sent_by_time ON sent (sent_at);
CREATE TABLE IF NOT EXISTS sources (
    name         TEXT PRIMARY KEY,
    last_success TEXT,
    failures     INTEGER NOT NULL DEFAULT 0,
    disabled     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS signatures (
    fingerprint TEXT PRIMARY KEY,
    seen_at     TEXT NOT NULL,
    minhash     TEXT NOT NULL,
    roles       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signatures_by_time ON signatures (seen_at);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT,
    sent_at     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    remote      TEXT NOT NULL DEFAULT '',
    salary      TEXT NOT NULL DEFAULT '',
    posted_at   TEXT,
    description TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_by_time ON jobs (sent_at);
CREATE TABLE IF NOT EXISTS applications (
    fingerprint TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    channel     TEXT,
    contact     TEXT,
    title       TEXT NOT NULL DEFAULT '',
    source_url  TEXT,
    score       INTEGER,
    reason      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS applications_by_time ON applications (at);
CREATE INDEX IF NOT EXISTS applications_by_mode ON applications (mode, status, at);
CREATE TABLE IF NOT EXISTS apply_queue (
    fingerprint TEXT PRIMARY KEY,
    posted_at   TEXT,
    queued_at   TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    contact TEXT NOT NULL,
    mode    TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (contact, mode)
);
CREATE TABLE IF NOT EXISTS apply_paused (
    channel TEXT PRIMARY KEY,
    until   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    delivered   INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_time ON runs (started_at);
CREATE TABLE IF NOT EXISTS bot_chats (
    chat_id  TEXT PRIMARY KEY,
    type     TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    seen_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_posts (
    chat_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    username   TEXT NOT NULL DEFAULT '',
    posted_at  TEXT NOT NULL,
    text       TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS bot_posts_by_user ON bot_posts (username, posted_at);
CREATE TABLE IF NOT EXISTS review_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    added_at    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    posting     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    resolved_at TEXT,
    resolved_via TEXT
);
CREATE INDEX IF NOT EXISTS review_by_status ON review_queue (status, added_at);
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,              -- telegram / email
    contact         TEXT NOT NULL,              -- "@handle" or "name@company.com" (lower-case)
    display_name    TEXT NOT NULL DEFAULT '',
    peer_id         TEXT,                       -- Telegram user id, once known
    created_at      TEXT NOT NULL,
    last_message_at TEXT,
    unread          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel, contact)
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    direction       TEXT NOT NULL,              -- out (you) / in (them)
    sent_at         TEXT NOT NULL,
    text            TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    attachment      TEXT NOT NULL DEFAULT '',   -- file name, e.g. resume.pdf
    external_id     TEXT,                       -- Telegram message id / email Message-ID
    job_fingerprint TEXT,
    job_title       TEXT NOT NULL DEFAULT '',
    UNIQUE (conversation_id, external_id)
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages (conversation_id, sent_at);
"""
RUNS_KEPT = 500

_APP_FIELDS = ("at", "mode", "status", "channel", "contact", "title", "source_url", "message",
               "score", "reason", "error")
_APP_TEXT_FIELDS = ("title", "reason", "error", "message")  # NOT NULL, default ""


def _pid_alive(pid: int) -> bool:
    """Is a process with this id running on this machine? (Never sends a
    signal: on Windows os.kill(pid, 0) would deliver Ctrl+C.)"""
    import os

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class State:
    def __init__(self, path: str | Path, *, max_sent: int = 5000, repost_window_days: int = 60,
                 legacy_json: str | Path | None = None):
        self.path = Path(path)
        self.max_sent = max_sent
        self.repost_window_days = repost_window_days
        self._lock = threading.RLock()
        self._repost_index = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = self._open()
        if legacy_json:
            self._migrate_json(Path(legacy_json))

    # ------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False,
                             isolation_level=None)  # autocommit; transactions are explicit
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=10000")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"{self.path.name} was written by a newer Job Radar (schema {version}); update Job Radar")
            db.executescript(_SCHEMA)
            app_cols = {r[1] for r in db.execute("PRAGMA table_info(applications)")}
            if "message" not in app_cols:  # schema 5: the exact text sent/drafted
                db.execute("ALTER TABLE applications ADD COLUMN message TEXT NOT NULL DEFAULT ''")
            if version < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            db.close()
            raise
        return db

    def _open(self) -> sqlite3.Connection:
        try:
            return self._connect()
        except sqlite3.DatabaseError as e:
            if "newer Job Radar" in str(e) or not self.path.exists():
                raise
            # A corrupt database must not stop collection: set it aside and start
            # fresh (worst case: some recent jobs are sent once more).
            aside = self.path.with_name(f"{self.path.name}.corrupt-{utcnow():%Y%m%d%H%M%S}")
            self.path.rename(aside)
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            log.warning("state database was unreadable (%s); moved it to %s and started fresh", e, aside.name)
            return self._connect()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __del__(self) -> None:  # tests and short-lived CLI commands just drop it
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass

    def save(self) -> None:
        """Changes are committed as they happen; this just enforces the size
        limits (kept for callers written against the old JSON state)."""
        self._prune()

    def _prune(self) -> None:
        keep = self.max_sent
        with self._tx() as db:
            for table, col, key in (("sent", "sent_at", "fingerprint"),
                                    ("applications", "at", "fingerprint"),
                                    ("jobs", "sent_at", "id")):
                db.execute(f"DELETE FROM {table} WHERE {key} NOT IN "
                           f"(SELECT {key} FROM {table} ORDER BY {col} DESC LIMIT ?)", (keep,))
            before = db.total_changes
            if self.repost_window_days > 0:
                cutoff = iso(utcnow() - timedelta(days=self.repost_window_days))
                db.execute("DELETE FROM signatures WHERE seen_at < ?", (cutoff,))
            else:
                db.execute("DELETE FROM signatures")
            db.execute("DELETE FROM signatures WHERE fingerprint NOT IN "
                       "(SELECT fingerprint FROM signatures ORDER BY seen_at DESC LIMIT ?)", (keep,))
            if db.total_changes != before:
                self._repost_index = None  # rebuilt on next use

    # ---------------------------------------------------------- run history
    def record_run(self, summary: dict[str, Any]) -> None:
        """Store one check's summary (SRS FR-38: per run and per source)."""
        errors = sum(1 for s in summary.get("sources", []) if s.get("status") != "ok")
        with self._tx() as db:
            db.execute("INSERT INTO runs (trigger, started_at, finished_at, fetched, delivered, errors, summary) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (summary.get("trigger", ""), summary.get("at") or iso(utcnow()), summary.get("finished"),
                        summary.get("fetched", 0), summary.get("delivered", 0), errors,
                        json.dumps(summary, ensure_ascii=False)))
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (RUNS_KEPT,))

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._q("SELECT id, trigger, started_at, finished_at, fetched, delivered, errors "
                       "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def last_run_summary(self) -> dict[str, Any] | None:
        row = self._one("SELECT summary FROM runs ORDER BY id DESC LIMIT 1")
        return json.loads(row["summary"]) if row else None

    # ------------------------------------------------------------ bot inbox
    def meta_get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def remember_chat(self, chat: dict[str, Any]) -> None:
        name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO bot_chats VALUES (?, ?, ?, ?, ?)",
                       (str(chat.get("id")), chat.get("type") or "", name or chat.get("username") or "",
                        chat.get("username") or "", iso(utcnow())))

    def bot_chats(self) -> list[dict[str, Any]]:
        return [{"id": r["chat_id"], "type": r["type"], "name": r["name"] or r["chat_id"], "username": r["username"]}
                for r in self._q("SELECT * FROM bot_chats ORDER BY seen_at DESC")]

    def add_bot_post(self, chat_id: str, message_id: int, username: str, posted_at: str, text: str) -> None:
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO bot_posts VALUES (?, ?, ?, ?, ?)",
                       (str(chat_id), int(message_id), username.lower(), posted_at, text))
            db.execute("DELETE FROM bot_posts WHERE posted_at < ?", (iso(utcnow() - timedelta(days=14)),))

    def bot_posts(self, channel: str, since: datetime | None = None) -> list[dict[str, Any]]:
        """Channel posts the bot received (it must be in the channel), newest last."""
        ch = channel.lstrip("@").lower()
        rows = self._q("SELECT * FROM bot_posts WHERE (username = ? OR chat_id = ?) AND posted_at > ? "
                       "ORDER BY posted_at", (ch, channel, iso(since) if since else ""))
        return [dict(r) for r in rows]

    # --------------------------------------------------------- review queue
    def review_add(self, fingerprint: str, posting: dict[str, Any]) -> int | None:
        """Queue a job for your review (SRS FR-21). Returns its id, or None if
        it was queued before (pending or already decided)."""
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO review_queue (fingerprint, added_at, source, title, url, posting) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (fingerprint, iso(utcnow()), ", ".join(posting.get("origins") or [posting.get("source") or ""]),
                 (posting.get("title") or "")[:300], posting.get("source_url") or "",
                 json.dumps(posting, ensure_ascii=False)))
            return cur.lastrowid if cur.rowcount else None

    def review_has(self, fingerprint: str) -> bool:
        return self._one("SELECT 1 FROM review_queue WHERE fingerprint = ?", (fingerprint,)) is not None

    def review_get(self, item_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM review_queue WHERE id = ?", (item_id,))
        if not row:
            return None
        d = dict(row)
        d["posting"] = json.loads(d["posting"])
        return d

    def review_pending(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._q("SELECT id, fingerprint, added_at, source, title, url, posting FROM review_queue "
                       "WHERE status = 'pending' ORDER BY added_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            p = json.loads(d.pop("posting"))
            d["excerpt"] = (p.get("description") or "")[:600]
            out.append(d)
        return out

    def review_resolve(self, item_id: int, status: str, via: str) -> bool:
        """Mark a pending item approved/rejected. False if it wasn't pending."""
        with self._tx() as db:
            cur = db.execute("UPDATE review_queue SET status = ?, resolved_at = ?, resolved_via = ? "
                             "WHERE id = ? AND status = 'pending'", (status, iso(utcnow()), via, item_id))
            return cur.rowcount == 1

    def review_reopen(self, item_id: int) -> None:
        with self._tx() as db:
            db.execute("UPDATE review_queue SET status = 'pending', resolved_at = NULL, resolved_via = NULL "
                       "WHERE id = ?", (item_id,))

    def review_expire(self, days: int) -> list[str]:
        """Expire items left unanswered for ``days`` (FR-25); returns their fingerprints."""
        cutoff = iso(utcnow() - timedelta(days=days))
        with self._tx() as db:
            rows = db.execute("SELECT fingerprint FROM review_queue WHERE status = 'pending' AND added_at < ?",
                              (cutoff,)).fetchall()
            db.execute("UPDATE review_queue SET status = 'expired', resolved_at = ?, resolved_via = 'expired' "
                       "WHERE status = 'pending' AND added_at < ?", (iso(utcnow()), cutoff))
            db.execute("DELETE FROM review_queue WHERE status != 'pending' AND resolved_at < ?",
                       (iso(utcnow() - timedelta(days=180)),))
        return [r["fingerprint"] for r in rows]

    def review_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._q("SELECT status, COUNT(*) AS n FROM review_queue GROUP BY status")}

    def review_by_source(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self._q("SELECT source, status, COUNT(*) AS n FROM review_queue "
                         "WHERE status IN ('approved', 'rejected') GROUP BY source, status"):
            out.setdefault(r["source"], {"approved": 0, "rejected": 0})[r["status"]] = r["n"]
        return out

    # --------------------------------------------------------- conversations
    def conversation_id(self, channel: str, contact: str, *, display_name: str | None = None,
                        peer_id: str | None = None) -> int:
        """The conversation with one representative (created if new)."""
        key = contact.strip().lower()
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO conversations (channel, contact, created_at) VALUES (?, ?, ?)",
                       (channel, key, iso(utcnow())))
            if display_name:
                db.execute("UPDATE conversations SET display_name = ? WHERE channel = ? AND contact = ?",
                           (display_name, channel, key))
            if peer_id:
                db.execute("UPDATE conversations SET peer_id = ? WHERE channel = ? AND contact = ?",
                           (str(peer_id), channel, key))
            return db.execute("SELECT id FROM conversations WHERE channel = ? AND contact = ?",
                              (channel, key)).fetchone()["id"]

    def add_message(self, conversation_id: int, direction: str, text: str, *, sent_at: datetime | None = None,
                    external_id: str | None = None, attachment: str = "", subject: str = "",
                    job_fingerprint: str | None = None, job_title: str = "", unread: bool = False) -> bool:
        """Store one message. Returns False if it was already stored (same id)."""
        when = iso(sent_at or utcnow())
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO messages (conversation_id, direction, sent_at, text, subject, attachment, "
                "external_id, job_fingerprint, job_title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, direction, when, text or "", subject or "", attachment or "",
                 external_id, job_fingerprint, job_title or ""))
            if not cur.rowcount:
                return False
            db.execute("UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at, ''), ?), "
                       "unread = unread + ? WHERE id = ?", (when, 1 if unread else 0, conversation_id))
            return True

    def conversations(self, channel: str | None = None) -> list[dict[str, Any]]:
        """Every conversation, most recent first, with its last message."""
        rows = self._q(
            "SELECT c.*, (SELECT text FROM messages m WHERE m.conversation_id = c.id ORDER BY sent_at DESC, id DESC "
            "LIMIT 1) AS last_text, (SELECT direction FROM messages m WHERE m.conversation_id = c.id "
            "ORDER BY sent_at DESC, id DESC LIMIT 1) AS last_direction, (SELECT job_title FROM messages m "
            "WHERE m.conversation_id = c.id AND job_title != '' ORDER BY sent_at DESC LIMIT 1) AS job_title, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c WHERE (? IS NULL OR c.channel = ?) "
            "ORDER BY COALESCE(c.last_message_at, c.created_at) DESC", (channel, channel))
        return [dict(r) for r in rows]

    def conversation(self, conversation_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if not row:
            return None
        d = dict(row)
        d["messages"] = [dict(r) for r in self._q(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sent_at, id", (conversation_id,))]
        return d

    def mark_read(self, conversation_id: int) -> None:
        with self._tx() as db:
            db.execute("UPDATE conversations SET unread = 0 WHERE id = ?", (conversation_id,))

    def unread_total(self) -> int:
        return self._one("SELECT COALESCE(SUM(unread), 0) AS n FROM conversations")["n"]

    def last_telegram_message_id(self, conversation_id: int) -> int:
        row = self._one("SELECT MAX(CAST(external_id AS INTEGER)) AS n FROM messages "
                        "WHERE conversation_id = ? AND external_id GLOB '[0-9]*'", (conversation_id,))
        return int(row["n"] or 0)

    # ------------------------------------------------------------- run lock
    def acquire_run_lock(self, owner: str, *, ttl: timedelta = timedelta(hours=3)) -> bool:
        """Only one check at a time across processes (e.g. the dashboard and
        `jobradar serve` sharing this database). A lock whose process has died,
        or that is older than ``ttl``, is taken over."""
        import os
        import socket

        me = {"owner": owner, "pid": os.getpid(), "host": socket.gethostname(), "at": iso(utcnow())}
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'run_lock'").fetchone()
            if row:
                try:
                    held = json.loads(row["value"])
                except (ValueError, TypeError):
                    held = {}
                fresh = (parse_iso(held.get("at")) or utcnow() - ttl * 2) > utcnow() - ttl
                same_host = held.get("host") == me["host"]
                alive = not same_host or _pid_alive(int(held.get("pid") or 0))
                if held.get("owner") != owner and fresh and alive:
                    return False
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('run_lock', ?)", (json.dumps(me),))
        return True

    def release_run_lock(self, owner: str) -> None:
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'run_lock'").fetchone()
            if row and json.loads(row["value"]).get("owner") == owner:
                db.execute("DELETE FROM meta WHERE key = 'run_lock'")

    def run_lock_holder(self) -> dict[str, Any] | None:
        row = self._one("SELECT value FROM meta WHERE key = 'run_lock'")
        return json.loads(row["value"]) if row else None

    # ------------------------------------------------------------ last run
    @property
    def last_run(self) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = 'last_run'")
        return row["value"] if row else None

    def mark_run(self, when: datetime | None = None) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('last_run', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (iso(when or utcnow()),))

    # ---------------------------------------------------- sent fingerprints
    def already_sent(self, fingerprint: str) -> bool:
        return self._one("SELECT 1 FROM sent WHERE fingerprint = ?", (fingerprint,)) is not None

    def mark_sent(self, fingerprint: str, when: datetime | None = None) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO sent (fingerprint, sent_at) VALUES (?, ?)",
                       (fingerprint, iso(when or utcnow())))

    def sent_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM sent")["n"]

    # ------------------------------------------------------- delivered jobs
    def record_job(self, job: dict[str, Any]) -> None:
        """Store a delivered job, including its full original text."""
        with self._tx() as db:
            db.execute(
                "INSERT INTO jobs (fingerprint, sent_at, title, url, source, location, remote, "
                "salary, posted_at, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job.get("fingerprint"), job.get("at") or iso(utcnow()), job.get("title") or "",
                 job.get("url") or "", job.get("source") or "", job.get("location") or "",
                 job.get("remote") or "", job.get("salary") or "", job.get("posted_at"),
                 job.get("description") or ""))

    def recent_jobs(self, limit: int = 50, *, with_text: bool = False) -> list[dict[str, Any]]:
        cols = "fingerprint, sent_at AS at, title, url, source, location, remote, salary, posted_at"
        if with_text:
            cols += ", description"
        return [dict(r) for r in self._q(f"SELECT {cols} FROM jobs ORDER BY sent_at DESC, id DESC LIMIT ?",
                                         (limit,))]

    # ---------------------------------------- near-duplicate (repost) memory
    def remember_signature(self, fingerprint: str, signature: str, roles: str,
                           when: datetime | None = None) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO signatures (fingerprint, seen_at, minhash, roles) "
                       "VALUES (?, ?, ?, ?)", (fingerprint, iso(when or utcnow()), signature, roles))
        if self._repost_index is not None:
            self._repost_index.add(fingerprint, signature, roles)

    def find_repost(self, signature: str, roles, *, threshold: float = 0.6) -> str | None:
        """Fingerprint of a remembered sent posting this one is a reworded copy of."""
        if self.repost_window_days <= 0:
            return None
        if self._repost_index is None:
            from .dedup import RepostIndex

            self._prune()
            rows = self._q("SELECT fingerprint, minhash, roles FROM signatures")
            self._repost_index = RepostIndex({r["fingerprint"]: (r["minhash"], r["roles"]) for r in rows})
        if not len(self._repost_index):
            return None
        return self._repost_index.find(signature, roles, threshold=threshold)

    def signature_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM signatures")["n"]

    # --------------------------------------------------- per-source state
    def source(self, name: str) -> dict[str, Any]:
        row = self._one("SELECT last_success, failures, disabled FROM sources WHERE name = ?", (name,))
        if row is None:
            return {"last_success": None, "failures": 0, "disabled": False}
        return {"last_success": row["last_success"], "failures": row["failures"],
                "disabled": bool(row["disabled"])}

    def sources_info(self) -> dict[str, dict[str, Any]]:
        return {r["name"]: {"last_success": r["last_success"], "failures": r["failures"],
                            "disabled": bool(r["disabled"])}
                for r in self._q("SELECT * FROM sources")}

    def _upsert_source(self, name: str, assignments: str, args: tuple = ()) -> None:
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (name,))
            db.execute(f"UPDATE sources SET {assignments} WHERE name = ?", (*args, name))

    def source_last_success(self, name: str) -> datetime | None:
        return parse_iso(self.source(name)["last_success"])

    def record_success(self, name: str, when: datetime | None = None) -> None:
        self._upsert_source(name, "last_success = ?, failures = 0", (iso(when or utcnow()),))

    def record_failure(self, name: str) -> int:
        self._upsert_source(name, "failures = failures + 1")
        return self.source(name)["failures"]

    def disable_source(self, name: str) -> None:
        self._upsert_source(name, "disabled = 1")

    def is_disabled(self, name: str) -> bool:
        return self.source(name)["disabled"]

    def enable_source(self, name: str) -> None:
        self._upsert_source(name, "disabled = 0, failures = 0")

    # ------------------------------------------------- auto-apply records
    def has_application(self, fingerprint: str) -> bool:
        return self._one("SELECT 1 FROM applications WHERE fingerprint = ?", (fingerprint,)) is not None

    def get_application(self, fingerprint: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM applications WHERE fingerprint = ?", (fingerprint,))
        return dict(row) if row else None

    def finish_application(self, fingerprint: str, record: dict[str, Any]) -> None:
        """Record an application's outcome and take it off the queue, atomically."""
        values = [(record.get(f) or "") if f in _APP_TEXT_FIELDS else record.get(f) for f in _APP_FIELDS]
        with self._tx() as db:
            db.execute(f"INSERT OR REPLACE INTO applications (fingerprint, {', '.join(_APP_FIELDS)}) "
                       f"VALUES (?{', ?' * len(_APP_FIELDS)})", (fingerprint, *values))
            db.execute("DELETE FROM apply_queue WHERE fingerprint = ?", (fingerprint,))

    def list_applications(self, limit: int = 300) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM applications ORDER BY at DESC LIMIT ?", (limit,))]

    def count_applications(self, *, mode: str, statuses: tuple[str, ...], since: datetime) -> int:
        marks = ", ".join("?" * len(statuses))
        row = self._one(f"SELECT COUNT(*) AS n FROM applications WHERE mode = ? AND status IN ({marks}) "
                        f"AND at > ?", (mode, *statuses, iso(since)))
        return row["n"]

    def application_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._q("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}

    # ------------------------------------------------------- apply queue
    def queue_has(self, fingerprint: str) -> bool:
        return self._one("SELECT 1 FROM apply_queue WHERE fingerprint = ?", (fingerprint,)) is not None

    def queue_put(self, fingerprint: str, entry: dict[str, Any]) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO apply_queue (fingerprint, posted_at, queued_at, data) "
                       "VALUES (?, ?, ?, ?)",
                       (fingerprint, entry.get("posted_at"), entry.get("queued_at") or iso(utcnow()),
                        json.dumps(entry, ensure_ascii=False)))

    def queue_items(self) -> list[tuple[str, dict[str, Any]]]:
        """Queued entries, newest posting first."""
        rows = self._q("SELECT fingerprint, data FROM apply_queue ORDER BY posted_at DESC")
        return [(r["fingerprint"], json.loads(r["data"])) for r in rows]

    def queue_remove(self, fingerprint: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM apply_queue WHERE fingerprint = ?", (fingerprint,))

    def queue_len(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM apply_queue")["n"]

    # ------------------------------------------------ contacts / pauses
    def contact_last(self, contact: str, mode: str) -> str | None:
        row = self._one("SELECT at FROM contacts WHERE contact = ? AND mode = ?", (contact, mode))
        return row["at"] if row else None

    def mark_contact(self, contact: str, mode: str, when: datetime | None = None) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO contacts (contact, mode, at) VALUES (?, ?, ?)",
                       (contact, mode, iso(when or utcnow())))

    def paused_until(self, channel: str) -> str | None:
        row = self._one("SELECT until FROM apply_paused WHERE channel = ?", (channel,))
        return row["until"] if row else None

    def set_paused(self, channel: str, until: str) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO apply_paused (channel, until) VALUES (?, ?)", (channel, until))

    def clear_paused(self, channel: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM apply_paused WHERE channel = ?", (channel,))

    def paused_channels(self) -> dict[str, str]:
        return {r["channel"]: r["until"] for r in self._q("SELECT channel, until FROM apply_paused")}

    # ---------------------------------------------- one-time JSON import
    def _migrate_json(self, legacy: Path) -> None:
        """Import the pre-SQLite ``jobradar-state.json`` once, then rename it."""
        if not legacy.exists() or legacy.resolve() == self.path.resolve():
            return
        if self._one("SELECT 1 FROM meta WHERE key = 'migrated_from_json'"):
            return
        try:
            data = json.loads(legacy.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("couldn't read old state file %s (%s); starting fresh", legacy.name, e)
            return
        with self._tx() as db:
            db.executemany("INSERT OR IGNORE INTO sent VALUES (?, ?)",
                           [(k, v) for k, v in (data.get("sent") or {}).items() if isinstance(v, str)])
            for name, s in (data.get("sources") or {}).items():
                db.execute("INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?)",
                           (name, s.get("last_success"), int(s.get("failures") or 0), int(bool(s.get("disabled")))))
            if data.get("last_run"):
                db.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (data["last_run"],))
            for fp, v in (data.get("signatures") or {}).items():
                if isinstance(v, list) and len(v) == 3:
                    db.execute("INSERT OR IGNORE INTO signatures VALUES (?, ?, ?, ?)", (fp, *v))
            for r in reversed(data.get("recent") or []):
                if isinstance(r, dict):
                    db.execute("INSERT INTO jobs (sent_at, title, url, source, location, remote, salary) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (r.get("at") or iso(utcnow()), r.get("title") or "", r.get("url") or "",
                                r.get("source") or "", r.get("location") or "", r.get("remote") or "",
                                r.get("salary") or ""))
            for fp, rec in (data.get("applications") or {}).items():
                if isinstance(rec, dict) and rec.get("at"):
                    db.execute(f"INSERT OR IGNORE INTO applications (fingerprint, {', '.join(_APP_FIELDS)}) "
                               f"VALUES (?{', ?' * len(_APP_FIELDS)})",
                               (fp, *[(rec.get(f) or "") if f in _APP_TEXT_FIELDS else rec.get(f)
                                      for f in _APP_FIELDS]))
            for fp, e in (data.get("apply_queue") or {}).items():
                if isinstance(e, dict):
                    db.execute("INSERT OR IGNORE INTO apply_queue VALUES (?, ?, ?, ?)",
                               (fp, e.get("posted_at"), e.get("queued_at") or iso(utcnow()),
                                json.dumps(e, ensure_ascii=False)))
            for contact, modes in (data.get("contacts") or {}).items():
                for mode, at in (modes or {}).items():
                    db.execute("INSERT OR IGNORE INTO contacts VALUES (?, ?, ?)", (contact, mode, at))
            for ch, until in (data.get("apply_paused") or {}).items():
                if until:
                    db.execute("INSERT OR REPLACE INTO apply_paused VALUES (?, ?)", (ch, until))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_from_json', ?)", (iso(utcnow()),))
        done = legacy.with_name(legacy.name + ".migrated")
        try:
            legacy.rename(done)
        except OSError:
            pass
        log.info("moved %d sent jobs and the rest of %s into %s (old file kept as %s)",
                 len(data.get("sent") or {}), legacy.name, self.path.name, done.name)
