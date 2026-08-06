"""Storage layer.

A single-file SQLite database (DR-1, DR-2). WAL mode plus per-run transactions
give the crash-safety the intermittent host demands: an interrupted run leaves
no partial records and the next startup resumes cleanly (NFR-4, AC-9).

The schema separates a *global* posting (collected once) from a *per-user*
view of it (``user_postings``). That separation is what lets two members with
different keyword sets receive demonstrably different digests (AC-6) while a
job posted to two sources is still stored a single time (FR-27, AC-5).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    is_owner           INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id   TEXT,
    telegram_link_code TEXT,
    dashboard_token    TEXT,                            -- IR-5 web login token
    keywords           TEXT NOT NULL DEFAULT '[]',      -- JSON list
    eligible_countries TEXT NOT NULL DEFAULT '[]',      -- JSON list of ISO codes
    remote_only        INTEGER NOT NULL DEFAULT 1,
    muted_until        TEXT,                            -- ISO timestamp or NULL
    created_at         TEXT NOT NULL,
    deleted            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    name                 TEXT PRIMARY KEY,
    type                 TEXT NOT NULL,                 -- reddit/bluesky/hn/rss/telegram/scrape
    tier                 TEXT NOT NULL,                 -- A / B
    enabled              INTEGER NOT NULL DEFAULT 1,
    blocked              INTEGER NOT NULL DEFAULT 0,    -- NFR-18: permanent until re-enabled
    config               TEXT NOT NULL DEFAULT '{}',    -- JSON collector params
    request_interval_s   REAL,                          -- SR-6 override
    last_success_at      TEXT,                          -- FR-3
    consecutive_failures INTEGER NOT NULL DEFAULT 0     -- SR-4
);

-- FR-6: every raw fetched item is stored before parsing so extraction can be
-- re-run against history without re-fetching.
CREATE TABLE IF NOT EXISTS raw_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url         TEXT,
    raw_text    TEXT NOT NULL,
    raw_json    TEXT,
    title_hint  TEXT,
    location_hint TEXT,
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0,
    is_posting  INTEGER,                                -- NULL until classified
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS postings (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,                      -- FR-12: unmodified source text
    contact         TEXT,                               -- DR-4: personal data, minimise
    location        TEXT,
    is_remote       TEXT NOT NULL DEFAULT 'unknown',    -- remote/hybrid/onsite/unknown
    hiring_countries TEXT NOT NULL DEFAULT '[]',        -- JSON list of ISO codes
    is_worldwide    INTEGER NOT NULL DEFAULT 0,         -- FR-19
    salary_raw      TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_period   TEXT,
    source          TEXT NOT NULL,
    source_tier     TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    origins         TEXT NOT NULL DEFAULT '[]',         -- FR-29: all merged sources
    content_hash    TEXT NOT NULL,                      -- dedup shingle signature
    apply_url       TEXT,                               -- normalised application URL
    posted_at       TEXT NOT NULL,
    collected_at    TEXT NOT NULL,
    duplicate_of    TEXT                                -- FR-27/28: canonical id
);
CREATE INDEX IF NOT EXISTS idx_postings_posted_at ON postings(posted_at);
CREATE INDEX IF NOT EXISTS idx_postings_dupe ON postings(duplicate_of);

-- Per-user projection of a posting: status and matched keywords differ per
-- member (AC-6, NFR-8 isolation).
CREATE TABLE IF NOT EXISTS user_postings (
    user_id          TEXT NOT NULL,
    posting_id       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'new',       -- new/pending_review/delivered/applied/saved/dismissed/expired
    matched_keywords TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    delivered_at     TEXT,
    resolved_at      TEXT,
    PRIMARY KEY (user_id, posting_id)
);
CREATE INDEX IF NOT EXISTS idx_up_status ON user_postings(user_id, status);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger      TEXT NOT NULL,                         -- scheduled/manual/catchup
    window_start TEXT,
    window_end   TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS run_sources (
    run_id            INTEGER NOT NULL,
    source            TEXT NOT NULL,
    items_fetched     INTEGER NOT NULL DEFAULT 0,       -- FR-38
    postings_detected INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ok',        -- ok/error/skipped
    error             TEXT,
    PRIMARY KEY (run_id, source)
);

-- FR-24: learn from review resolutions.
CREATE TABLE IF NOT EXISTS resolutions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    posting_id  TEXT NOT NULL,
    decision    TEXT NOT NULL,                          -- relevant / not_relevant
    source      TEXT,
    resolved_at TEXT NOT NULL
);

-- NFR-5: delivery is durable and retried; a posting survives Telegram outage.
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    run_id       INTEGER,
    payload      TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    delivered    INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);
"""


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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    """Thin wrapper around a SQLite connection with helpers and migrations."""

    def __init__(self, path: str | Path = "jobradar.db"):
        self.path = str(path)
        # check_same_thread=False so the threaded dashboard can read/write from
        # its worker threads. WAL + busy_timeout + autocommit statements keep
        # this safe at the SRS's scale (a handful of users, a single host).
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        # WAL + FULL sync: crash mid-run must never corrupt the file (NFR-4).
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        self._conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Atomic unit of work. Either the whole thing commits or none of it.

        Each source's contribution to a run is wrapped in one of these so an
        interrupted run leaves consistent state (AC-9).
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            yield cur
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    # ---- meta key/value ---------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def backup_to(self, dest: str | Path) -> None:
        """Consistent online backup to a local path (NFR-7)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            with target:
                self._conn.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        self._conn.close()


def load_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback
