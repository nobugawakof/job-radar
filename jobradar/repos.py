"""Repository layer — all SQL lives here.

Higher layers (pipeline, delivery, web) call these methods and never touch SQL
directly. Member isolation (NFR-8) is enforced here: every per-user query is
scoped by ``user_id``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Iterable

from .db import Database, iso, load_json, parse_iso, utcnow
from .extraction import Salary
from .models import (
    Posting,
    RawItem,
    STATUS_DELIVERED,
    STATUS_EXPIRED,
    STATUS_NEW,
    STATUS_PENDING_REVIEW,
    new_id,
)
from .filters import UserSettings


class Store:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ users
    def create_user(
        self,
        name: str,
        *,
        is_owner: bool = False,
        keywords: list[str] | None = None,
        eligible_countries: list[str] | None = None,
        remote_only: bool = True,
    ) -> str:
        uid = new_id()
        self.db.execute(
            "INSERT INTO users(id, name, is_owner, keywords, eligible_countries, remote_only, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                uid,
                name,
                1 if is_owner else 0,
                json.dumps(keywords or []),
                json.dumps(eligible_countries or []),
                1 if remote_only else 0,
                iso(utcnow()),
            ),
        )
        return uid

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM users WHERE id=? AND deleted=0", (user_id,))
        return self._user_dict(row) if row else None

    def get_user_by_chat_id(self, chat_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM users WHERE telegram_chat_id=? AND deleted=0", (str(chat_id),)
        )
        return self._user_dict(row) if row else None

    def list_users(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM users" + ("" if include_deleted else " WHERE deleted=0")
        return [self._user_dict(r) for r in self.db.query(sql + " ORDER BY created_at")]

    def user_settings(self, user_id: str) -> UserSettings | None:
        u = self.get_user(user_id)
        if not u:
            return None
        return UserSettings(
            id=u["id"],
            keywords=u["keywords"],
            eligible_countries=u["eligible_countries"],
            remote_only=u["remote_only"],
        )

    def update_user(self, user_id: str, **fields: Any) -> None:
        allowed = {
            "keywords", "eligible_countries", "remote_only", "muted_until",
            "telegram_chat_id", "telegram_link_code", "name",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("keywords", "eligible_countries"):
                v = json.dumps(v)
            elif k == "remote_only":
                v = 1 if v else 0
            elif k == "muted_until" and isinstance(v, datetime):
                v = iso(v)
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return
        params.append(user_id)
        self.db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(params))

    def issue_dashboard_token(self, user_id: str) -> str:
        """IR-5: per-member token for authenticating to the web dashboard."""
        token = secrets.token_urlsafe(24)
        self.db.execute("UPDATE users SET dashboard_token=? WHERE id=?", (token, user_id))
        return token

    def get_user_by_dashboard_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        row = self.db.query_one(
            "SELECT * FROM users WHERE dashboard_token=? AND deleted=0", (token,)
        )
        return self._user_dict(row) if row else None

    def issue_link_code(self, user_id: str) -> str:
        """IR-1: one-time code the owner hands a member to link Telegram."""
        code = secrets.token_hex(4)
        self.update_user(user_id, telegram_link_code=code)
        return code

    def link_telegram(self, code: str, chat_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM users WHERE telegram_link_code=? AND deleted=0", (code,)
        )
        if not row:
            return None
        self.db.execute(
            "UPDATE users SET telegram_chat_id=?, telegram_link_code=NULL WHERE id=?",
            (str(chat_id), row["id"]),
        )
        return self.get_user(row["id"])

    def delete_user(self, user_id: str, *, hard: bool = False) -> None:
        """DR-5: full deletion for an individual member on request."""
        if hard:
            self.db.execute("DELETE FROM user_postings WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM resolutions WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM deliveries WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        else:
            self.db.execute("UPDATE users SET deleted=1 WHERE id=?", (user_id,))

    def export_user(self, user_id: str) -> dict[str, Any]:
        """DR-5: full data export for a member.

        The ``contact`` field is personal data (DR-4); it is included in the
        member's *own* export but excluded from bulk/logging paths elsewhere.
        """
        u = self.get_user(user_id) or {}
        rows = self.db.query(
            "SELECT p.*, up.status, up.matched_keywords, up.created_at AS up_created "
            "FROM user_postings up JOIN postings p ON p.id=up.posting_id "
            "WHERE up.user_id=?",
            (user_id,),
        )
        postings = [dict(r) for r in rows]
        return {"user": u, "postings": postings}

    def _user_dict(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "is_owner": bool(row["is_owner"]),
            "telegram_chat_id": row["telegram_chat_id"],
            "telegram_link_code": row["telegram_link_code"],
            "dashboard_token": row["dashboard_token"],
            "keywords": load_json(row["keywords"], []),
            "eligible_countries": load_json(row["eligible_countries"], []),
            "remote_only": bool(row["remote_only"]),
            "muted_until": row["muted_until"],
            "created_at": row["created_at"],
        }

    def is_muted(self, user_id: str, now: datetime | None = None) -> bool:
        u = self.get_user(user_id)
        if not u or not u["muted_until"]:
            return False
        return (now or utcnow()) < parse_iso(u["muted_until"])

    # ---------------------------------------------------------------- sources
    def upsert_source(
        self,
        name: str,
        type_: str,
        tier: str,
        *,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
        request_interval_s: float | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO sources(name, type, tier, enabled, config, request_interval_s) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET type=excluded.type, tier=excluded.tier, "
            "enabled=excluded.enabled, config=excluded.config, "
            "request_interval_s=excluded.request_interval_s",
            (name, type_, tier, 1 if enabled else 0, json.dumps(config or {}), request_interval_s),
        )

    def get_source(self, name: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM sources WHERE name=?", (name,))
        return self._source_dict(row) if row else None

    def list_sources(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled=1 AND blocked=0"
        return [self._source_dict(r) for r in self.db.query(sql + " ORDER BY name")]

    def set_source_enabled(self, name: str, enabled: bool) -> None:
        self.db.execute("UPDATE sources SET enabled=? WHERE name=?", (1 if enabled else 0, name))
        if enabled:
            # Re-enabling clears the failure count and any block (NFR-18 manual re-enable).
            self.db.execute(
                "UPDATE sources SET consecutive_failures=0, blocked=0 WHERE name=?", (name,)
            )

    def block_source(self, name: str) -> None:
        """NFR-18: a source that has explicitly blocked us stays off until
        manually re-enabled."""
        self.db.execute("UPDATE sources SET blocked=1, enabled=0 WHERE name=?", (name,))

    def record_source_success(self, name: str, last_item_at: datetime) -> None:
        self.db.execute(
            "UPDATE sources SET last_success_at=?, consecutive_failures=0 WHERE name=?",
            (iso(last_item_at), name),
        )

    def record_source_failure(self, name: str) -> int:
        """Increment and return the source's consecutive-failure count (SR-4)."""
        self.db.execute(
            "UPDATE sources SET consecutive_failures=consecutive_failures+1 WHERE name=?",
            (name,),
        )
        row = self.db.query_one("SELECT consecutive_failures FROM sources WHERE name=?", (name,))
        return int(row["consecutive_failures"]) if row else 0

    def _source_dict(self, row: Any) -> dict[str, Any]:
        return {
            "name": row["name"],
            "type": row["type"],
            "tier": row["tier"],
            "enabled": bool(row["enabled"]),
            "blocked": bool(row["blocked"]),
            "config": load_json(row["config"], {}),
            "request_interval_s": row["request_interval_s"],
            "last_success_at": row["last_success_at"],
            "consecutive_failures": row["consecutive_failures"],
        }

    # -------------------------------------------------------------- raw items
    def store_raw_item(self, item: RawItem) -> int | None:
        """FR-6: persist a raw item before parsing. Returns row id, or None if
        this (source, external_id) was already stored (idempotent collection)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO raw_items(source, external_id, url, raw_text, raw_json, "
            "title_hint, location_hint, posted_at, fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item.source,
                item.external_id,
                item.url,
                item.raw_text,
                item.raw_json,
                item.title_hint,
                item.location_hint,
                iso(item.posted_at),
                iso(utcnow()),
            ),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)

    def raw_item_exists(self, source: str, external_id: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 FROM raw_items WHERE source=? AND external_id=?",
                (source, external_id),
            )
            is not None
        )

    def mark_raw_processed(self, raw_id: int, is_posting: bool) -> None:
        self.db.execute(
            "UPDATE raw_items SET processed=1, is_posting=? WHERE id=?",
            (1 if is_posting else 0, raw_id),
        )

    def purge_old_non_postings(self, retention_days: int, now: datetime | None = None) -> int:
        """FR-9: retain non-postings for a period, then purge."""
        cutoff = iso((now or utcnow()) - timedelta(days=retention_days))
        cur = self.db.execute(
            "DELETE FROM raw_items WHERE processed=1 AND is_posting=0 AND fetched_at < ?",
            (cutoff,),
        )
        return cur.rowcount

    # --------------------------------------------------------------- postings
    def insert_posting(self, p: Posting) -> None:
        self.db.execute(
            "INSERT INTO postings(id, title, description, contact, location, is_remote, "
            "hiring_countries, is_worldwide, salary_raw, salary_min, salary_max, salary_currency, "
            "salary_period, source, source_tier, source_url, origins, content_hash, apply_url, "
            "posted_at, collected_at, duplicate_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.id, p.title, p.description, p.contact, p.location, p.is_remote,
                json.dumps(p.hiring_countries), 1 if p.is_worldwide else 0,
                p.salary.raw, p.salary.min, p.salary.max, p.salary.currency, p.salary.period,
                p.source, p.source_tier, p.source_url, json.dumps(p.origins or [p.source]),
                p.content_hash, p.apply_url, iso(p.posted_at), iso(p.collected_at), p.duplicate_of,
            ),
        )

    def get_posting(self, posting_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM postings WHERE id=?", (posting_id,))
        return self._posting_dict(row) if row else None

    def recent_postings(self, since: datetime) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM postings WHERE duplicate_of IS NULL AND collected_at >= ? ORDER BY collected_at",
            (iso(since),),
        )
        return [self._posting_dict(r) for r in rows]

    def add_origin(self, posting_id: str, origin: str) -> None:
        """FR-29: retain all originating sources on the surviving record."""
        row = self.db.query_one("SELECT origins FROM postings WHERE id=?", (posting_id,))
        if not row:
            return
        origins = load_json(row["origins"], [])
        if origin not in origins:
            origins.append(origin)
            self.db.execute(
                "UPDATE postings SET origins=? WHERE id=?", (json.dumps(origins), posting_id)
            )

    def mark_duplicate(self, posting_id: str, canonical_id: str) -> None:
        self.db.execute(
            "UPDATE postings SET duplicate_of=? WHERE id=?", (canonical_id, posting_id)
        )

    def _posting_dict(self, row: Any) -> dict[str, Any]:
        d = dict(row)
        d["hiring_countries"] = load_json(row["hiring_countries"], [])
        d["origins"] = load_json(row["origins"], [])
        d["is_worldwide"] = bool(row["is_worldwide"])
        return d

    def purge_old_postings(self, retention_days: int, now: datetime | None = None) -> int:
        """DR-3: purge postings not applied/saved after the retention period."""
        cutoff = iso((now or utcnow()) - timedelta(days=retention_days))
        # Only purge a posting if no user has it applied/saved.
        cur = self.db.execute(
            "DELETE FROM postings WHERE collected_at < ? AND id NOT IN ("
            "  SELECT posting_id FROM user_postings WHERE status IN ('applied','saved')"
            ")",
            (cutoff,),
        )
        # Clean up dangling per-user rows.
        self.db.execute(
            "DELETE FROM user_postings WHERE posting_id NOT IN (SELECT id FROM postings)"
        )
        return cur.rowcount

    # ---------------------------------------------------------- user_postings
    def upsert_user_posting(
        self, user_id: str, posting_id: str, status: str, matched_keywords: list[str]
    ) -> None:
        self.db.execute(
            "INSERT INTO user_postings(user_id, posting_id, status, matched_keywords, created_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id, posting_id) DO NOTHING",
            (user_id, posting_id, status, json.dumps(matched_keywords), iso(utcnow())),
        )

    def user_has_posting(self, user_id: str, posting_id: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 FROM user_postings WHERE user_id=? AND posting_id=?",
                (user_id, posting_id),
            )
            is not None
        )

    def set_user_posting_status(
        self, user_id: str, posting_id: str, status: str, *, resolved: bool = False
    ) -> None:
        extra = ", resolved_at=?" if resolved else ""
        params: tuple[Any, ...]
        if resolved:
            params = (status, iso(utcnow()), user_id, posting_id)
        else:
            params = (status, user_id, posting_id)
        self.db.execute(
            f"UPDATE user_postings SET status=?{extra} WHERE user_id=? AND posting_id=?",
            params,
        )

    def mark_delivered(self, user_id: str, posting_ids: Iterable[str]) -> None:
        now = iso(utcnow())
        for pid in posting_ids:
            self.db.execute(
                "UPDATE user_postings SET status=?, delivered_at=? "
                "WHERE user_id=? AND posting_id=? AND status=?",
                (STATUS_DELIVERED, now, user_id, pid, STATUS_NEW),
            )

    def pending_delivery(self, user_id: str) -> list[dict[str, Any]]:
        """New (undelivered) postings for a user, most recent first."""
        rows = self.db.query(
            "SELECT p.*, up.matched_keywords FROM user_postings up "
            "JOIN postings p ON p.id=up.posting_id "
            "WHERE up.user_id=? AND up.status=? AND p.duplicate_of IS NULL "
            "ORDER BY p.posted_at DESC",
            (user_id, STATUS_NEW),
        )
        return [self._join_dict(r) for r in rows]

    def review_queue(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT p.*, up.matched_keywords FROM user_postings up "
            "JOIN postings p ON p.id=up.posting_id "
            "WHERE up.user_id=? AND up.status=? ORDER BY up.created_at",
            (user_id, STATUS_PENDING_REVIEW),
        )
        return [self._join_dict(r) for r in rows]

    def list_user_postings(
        self,
        user_id: str,
        *,
        source: str | None = None,
        keyword: str | None = None,
        has_salary: bool | None = None,
        statuses: list[str] | None = None,
        sort: str = "posted_at",
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        """FR-34: browse/filter/sort a user's own postings (NFR-8 isolation)."""
        clauses = ["up.user_id=?", "p.duplicate_of IS NULL"]
        params: list[Any] = [user_id]
        if source:
            clauses.append("(p.source=? OR p.origins LIKE ?)")
            params.extend([source, f'%"{source}"%'])
        if keyword:
            clauses.append("up.matched_keywords LIKE ?")
            params.append(f'%"{keyword}"%')
        if has_salary is True:
            clauses.append("p.salary_raw IS NOT NULL")
        elif has_salary is False:
            clauses.append("p.salary_raw IS NULL")
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"up.status IN ({placeholders})")
            params.extend(statuses)
        else:
            clauses.append("up.status != 'dismissed'")  # FR-35: dismissed never reappear

        sort_cols = {
            "posted_at": "p.posted_at",
            "collected_at": "p.collected_at",
            "source": "p.source",
            "salary": "p.salary_min",
            "title": "p.title",
        }
        col = sort_cols.get(sort, "p.posted_at")
        order = "DESC" if descending else "ASC"
        sql = (
            "SELECT p.*, up.status, up.matched_keywords FROM user_postings up "
            "JOIN postings p ON p.id=up.posting_id WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY {col} {order}"
        )
        return [self._join_dict(r) for r in self.db.query(sql, tuple(params))]

    def expire_stale_reviews(self, days: int, now: datetime | None = None) -> int:
        """FR-25: unresolved queue items expire after a configurable period."""
        cutoff = iso((now or utcnow()) - timedelta(days=days))
        cur = self.db.execute(
            "UPDATE user_postings SET status=? WHERE status=? AND created_at < ?",
            (STATUS_EXPIRED, STATUS_PENDING_REVIEW, cutoff),
        )
        return cur.rowcount

    def _join_dict(self, row: Any) -> dict[str, Any]:
        d = self._posting_dict(row)
        d["matched_keywords"] = load_json(row["matched_keywords"], [])
        if "status" in row.keys():
            d["status"] = row["status"]
        return d

    # ------------------------------------------------------------------- runs
    def start_run(self, trigger: str, window_start: datetime | None, window_end: datetime | None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(trigger, window_start, window_end, started_at) VALUES(?,?,?,?)",
            (trigger, iso(window_start), iso(window_end), iso(utcnow())),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int) -> None:
        self.db.execute("UPDATE runs SET finished_at=? WHERE id=?", (iso(utcnow()), run_id))

    def record_run_source(
        self,
        run_id: int,
        source: str,
        *,
        items_fetched: int = 0,
        postings_detected: int = 0,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO run_sources(run_id, source, items_fetched, postings_detected, status, error) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(run_id, source) DO UPDATE SET items_fetched=excluded.items_fetched, "
            "postings_detected=excluded.postings_detected, status=excluded.status, error=excluded.error",
            (run_id, source, items_fetched, postings_detected, status, error),
        )

    def run_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """FR-38: per-run/per-source items, postings, errors."""
        runs = self.db.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        out = []
        for r in runs:
            sources = self.db.query("SELECT * FROM run_sources WHERE run_id=?", (r["id"],))
            out.append({**dict(r), "sources": [dict(s) for s in sources]})
        return out

    # ------------------------------------------------------------ resolutions
    def record_resolution(
        self, user_id: str, posting_id: str, decision: str, source: str | None
    ) -> None:
        self.db.execute(
            "INSERT INTO resolutions(user_id, posting_id, decision, source, resolved_at) "
            "VALUES(?,?,?,?,?)",
            (user_id, posting_id, decision, source, iso(utcnow())),
        )

    def suggested_rules(self, user_id: str, min_rejections: int = 3) -> list[dict[str, Any]]:
        """FR-24: surface repeated rejections sharing an attribute as a
        suggested filter rule."""
        rows = self.db.query(
            "SELECT source, COUNT(*) AS n FROM resolutions "
            "WHERE user_id=? AND decision='not_relevant' AND source IS NOT NULL "
            "GROUP BY source HAVING n >= ? ORDER BY n DESC",
            (user_id, min_rejections),
        )
        return [
            {"attribute": "source", "value": r["source"], "count": r["n"],
             "suggestion": f"Mute source '{r['source']}' — rejected {r['n']} times"}
            for r in rows
        ]

    # ------------------------------------------------------------- deliveries
    def enqueue_delivery(self, user_id: str, run_id: int | None, payload: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO deliveries(user_id, run_id, payload, created_at) VALUES(?,?,?,?)",
            (user_id, run_id, json.dumps(payload), iso(utcnow())),
        )
        return int(cur.lastrowid)

    def pending_deliveries(self, max_attempts: int = 5) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM deliveries WHERE delivered=0 AND attempts < ? ORDER BY created_at",
            (max_attempts,),
        )
        return [{**dict(r), "payload": load_json(r["payload"], {})} for r in rows]

    def mark_delivery_result(self, delivery_id: int, ok: bool, error: str | None = None) -> None:
        if ok:
            self.db.execute(
                "UPDATE deliveries SET delivered=1, delivered_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
                (iso(utcnow()), delivery_id),
            )
        else:
            self.db.execute(
                "UPDATE deliveries SET attempts=attempts+1, last_error=? WHERE id=?",
                (error, delivery_id),
            )
