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


class State:
    """In-memory view of the state file, with explicit save().

    Structure on disk::

        {
          "sent":    {"<fingerprint>": "<iso sent-at>", ...},
          "sources": {"<name>": {"last_success": "<iso>", "failures": 0,
                                  "disabled": false}},
          "last_run": "<iso>"
        }
    """

    def __init__(self, path: str | Path, *, max_sent: int = 5000):
        self.path = Path(path)
        self.max_sent = max_sent
        self.sent: dict[str, str] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.last_run: str | None = None
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

    def save(self) -> None:
        self._prune()
        payload = {"sent": self.sent, "sources": self.sources, "last_run": self.last_run}
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
        if len(self.sent) <= self.max_sent:
            return
        # Keep the most recent `max_sent` fingerprints by sent-at.
        keep = sorted(self.sent.items(), key=lambda kv: kv[1], reverse=True)[: self.max_sent]
        self.sent = dict(keep)

    # ---- sent fingerprints ------------------------------------------------
    def already_sent(self, fingerprint: str) -> bool:
        return fingerprint in self.sent

    def mark_sent(self, fingerprint: str, when: datetime | None = None) -> None:
        self.sent[fingerprint] = iso(when or utcnow())  # type: ignore[assignment]

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
