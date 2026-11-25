"""Lightweight JSON state — the only persistence in the Telegram-only build.

There is deliberately no database. The single thing the bot must remember
between runs is *which postings it already sent*, so it does not re-send the
same jobs every run. That, plus a per-source last-success timestamp and a
consecutive-failure counter (so a broken scraper can back off), is all the
state there is.

The file is written atomically (temp file + ``os.replace``) so a crash or a
laptop shutdown mid-write can never corrupt it: the old file stays intact until
the new one is fully in place.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


RECENT_MAX = 100


class State:
    """In-memory view of the state file, with explicit save().

    Structure on disk::

        {
          "sent":    {"<fingerprint>": "<iso sent-at>", ...},
          "sources": {"<name>": {"last_success": "<iso>", "failures": 0,
                                  "disabled": false}},
          "last_run": "<iso>",
          "applications": {"<fingerprint>": {"at", "mode", "status", ...}},
          "apply_queue":  {"<fingerprint>": {posting fields awaiting apply}},
          "contacts":     {"tg:@handle": {"sent": "<iso>", "dry_run": "<iso>"}},
          "apply_paused": {"telegram": "<iso until>"},
          "signatures":   {"<fingerprint>": ["<iso sent-at>", "<minhash>", "<roles>"]}
        }
    """

    def __init__(self, path: str | Path, *, max_sent: int = 5000, repost_window_days: int = 60):
        self.path = Path(path)
        self.max_sent = max_sent
        self.repost_window_days = repost_window_days
        # Near-duplicate memory of sent postings (see dedup.RepostIndex).
        self.signatures: dict[str, list[str]] = {}
        self._repost_index = None
        # The last few delivered jobs (title/link only), for the dashboard.
        self.recent: list[dict[str, Any]] = []
        self.sent: dict[str, str] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.last_run: str | None = None
        # Auto-apply bookkeeping (see jobradar/apply/engine.py).
        self.applications: dict[str, dict[str, Any]] = {}
        self.apply_queue: dict[str, dict[str, Any]] = {}
        self.contacts: dict[str, dict[str, str]] = {}
        self.apply_paused: dict[str, str] = {}
        self._load()

    # ---- load / save ------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt/unreadable state file must not stop collection; start
            # fresh (worst case: some already-sent jobs are re-sent once).
            return
        self.sent = dict(data.get("sent", {}))
        self.sources = dict(data.get("sources", {}))
        self.last_run = data.get("last_run")
        self.applications = dict(data.get("applications", {}))
        self.apply_queue = dict(data.get("apply_queue", {}))
        self.contacts = dict(data.get("contacts", {}))
        self.apply_paused = dict(data.get("apply_paused", {}))
        self.signatures = {k: v for k, v in dict(data.get("signatures", {})).items()
                           if isinstance(v, list) and len(v) == 3}
        self.recent = [r for r in data.get("recent", []) if isinstance(r, dict)][:RECENT_MAX]

    def save(self) -> None:
        self._prune()
        payload = {"sent": self.sent, "sources": self.sources, "last_run": self.last_run,
                   "applications": self.applications, "apply_queue": self.apply_queue,
                   "contacts": self.contacts, "apply_paused": self.apply_paused,
                   "signatures": self.signatures, "recent": self.recent}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a mid-write crash never corrupts the file.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _prune(self) -> None:
        """Bound the sent-set so the file cannot grow without limit."""
        self._prune_applications()
        self._prune_signatures()
        if len(self.sent) <= self.max_sent:
            return
        # Keep the most recent `max_sent` fingerprints by sent-at.
        keep = sorted(self.sent.items(), key=lambda kv: kv[1], reverse=True)[: self.max_sent]
        self.sent = dict(keep)

    def _prune_signatures(self) -> None:
        from datetime import timedelta

        before = len(self.signatures)
        if self.repost_window_days > 0:
            cutoff = iso(utcnow() - timedelta(days=self.repost_window_days))
            self.signatures = {k: v for k, v in self.signatures.items() if v[0] >= cutoff}
        else:
            self.signatures = {}
        if len(self.signatures) > self.max_sent:
            keep = sorted(self.signatures.items(), key=lambda kv: kv[1][0], reverse=True)
            self.signatures = dict(keep[: self.max_sent])
        if len(self.signatures) != before:
            self._repost_index = None  # rebuilt on next use

    def _prune_applications(self) -> None:
        if len(self.applications) > self.max_sent:
            keep = sorted(self.applications.items(), key=lambda kv: kv[1].get("at", ""),
                          reverse=True)[: self.max_sent]
            self.applications = dict(keep)

    # ---- sent fingerprints ------------------------------------------------
    def already_sent(self, fingerprint: str) -> bool:
        return fingerprint in self.sent

    def mark_sent(self, fingerprint: str, when: datetime | None = None) -> None:
        self.sent[fingerprint] = iso(when or utcnow())  # type: ignore[assignment]

    def add_recent(self, entry: dict[str, Any]) -> None:
        self.recent.insert(0, entry)
        del self.recent[RECENT_MAX:]

    # ---- near-duplicate (repost) memory ------------------------------------
    def remember_signature(self, fingerprint: str, signature: str, roles: str,
                           when: datetime | None = None) -> None:
        self.signatures[fingerprint] = [iso(when or utcnow()), signature, roles]  # type: ignore[list-item]
        if self._repost_index is not None:
            self._repost_index.add(fingerprint, signature, roles)

    def find_repost(self, signature: str, roles, *, threshold: float = 0.6) -> str | None:
        """Fingerprint of a remembered sent posting this one is a reworded copy of."""
        if self.repost_window_days <= 0 or not self.signatures:
            return None
        if self._repost_index is None:
            from .dedup import RepostIndex

            self._prune_signatures()
            self._repost_index = RepostIndex({k: (v[1], v[2]) for k, v in self.signatures.items()})
        return self._repost_index.find(signature, roles, threshold=threshold)

    # ---- per-source bookkeeping ------------------------------------------
    def source(self, name: str) -> dict[str, Any]:
        return self.sources.setdefault(
            name, {"last_success": None, "failures": 0, "disabled": False}
        )

    def source_last_success(self, name: str) -> datetime | None:
        return parse_iso(self.source(name).get("last_success"))

    def record_success(self, name: str, when: datetime | None = None) -> None:
        s = self.source(name)
        s["last_success"] = iso(when or utcnow())
        s["failures"] = 0

    def record_failure(self, name: str) -> int:
        s = self.source(name)
        s["failures"] = int(s.get("failures", 0)) + 1
        return s["failures"]

    def disable_source(self, name: str) -> None:
        self.source(name)["disabled"] = True

    def is_disabled(self, name: str) -> bool:
        return bool(self.source(name).get("disabled"))

    def enable_source(self, name: str) -> None:
        s = self.source(name)
        s["disabled"] = False
        s["failures"] = 0

    def mark_run(self, when: datetime | None = None) -> None:
        self.last_run = iso(when or utcnow())
