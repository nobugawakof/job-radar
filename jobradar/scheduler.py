"""Scheduler.

The host is a personal laptop that sleeps and disconnects unpredictably (SRS
2.3), so the scheduler is built around that fact rather than treating it as an
edge case:

* On startup it checks whether a scheduled collection window was missed while
  the host was asleep or offline and, if so, immediately performs a catch-up
  run (FR-4, AC-2) instead of waiting a full interval.
* Every run's per-source window is bounded by the catch-up lookback cap (FR-5),
  which is enforced in the pipeline, so even a long outage cannot trigger an
  unbounded back-fill.
* It also drives periodic housekeeping (expiry/purge) and local DB backups
  (NFR-7).

The loop uses only ``time.sleep`` and monotonic time; nothing here depends on
staying up continuously.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from .config import Config
from .db import iso, parse_iso, utcnow
from .pipeline import Pipeline, RunSummary
from .repos import Store

log = logging.getLogger("jobradar.scheduler")

_LAST_RUN_KEY = "scheduler.last_run_at"
_LAST_BACKUP_KEY = "scheduler.last_backup_at"


class Scheduler:
    def __init__(self, store: Store, config: Config, pipeline: Pipeline):
        self.store = store
        self.config = config
        self.pipeline = pipeline

    # ------------------------------------------------------------- catch-up
    def last_run_at(self) -> datetime | None:
        return parse_iso(self.store.db.get_meta(_LAST_RUN_KEY))

    def is_catchup_due(self, now: datetime | None = None) -> bool:
        """FR-4: true if a scheduled window was missed since the last run."""
        now = now or utcnow()
        last = self.last_run_at()
        if last is None:
            return True  # never run → the first startup run is a catch-up
        return (now - last) >= timedelta(hours=self.config.run_interval_hours)

    def _mark_ran(self, now: datetime) -> None:
        self.store.db.set_meta(_LAST_RUN_KEY, iso(now))

    def startup(self, now: datetime | None = None) -> RunSummary | None:
        """Run once at process start if a window was missed (FR-4)."""
        now = now or utcnow()
        if self.is_catchup_due(now):
            gap = None
            last = self.last_run_at()
            if last is not None:
                gap_hours = (now - last).total_seconds() / 3600
                log.info("catch-up run: %.1fh since last run", gap_hours)
            summary = self.pipeline.run(trigger="catchup")
            self._mark_ran(now)
            self._maybe_backup(now)
            self.pipeline.housekeeping(now)
            return summary
        return None

    # ---------------------------------------------------------- scheduled
    def run_once(self, trigger: str = "scheduled", now: datetime | None = None) -> RunSummary:
        now = now or utcnow()
        summary = self.pipeline.run(trigger=trigger)
        self._mark_ran(now)
        self._maybe_backup(now)
        self.pipeline.housekeeping(now)
        return summary

    def seconds_until_next(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        last = self.last_run_at() or now
        due = last + timedelta(hours=self.config.run_interval_hours)
        return max(0.0, (due - now).total_seconds())

    # ------------------------------------------------------------- backups
    def _maybe_backup(self, now: datetime) -> None:
        last = parse_iso(self.store.db.get_meta(_LAST_BACKUP_KEY))
        if last is not None and (now - last) < timedelta(hours=self.config.backup_interval_hours):
            return
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        dest = f"{self.config.backup_dir}/jobradar-{stamp}.db"
        try:
            self.store.db.backup_to(dest)
            self.store.db.set_meta(_LAST_BACKUP_KEY, iso(now))
            log.info("backed up database to %s", dest)
        except Exception:  # noqa: BLE001 - a failed backup must not stop collection
            log.exception("database backup failed")

    # -------------------------------------------------------------- daemon
    def run_forever(self, *, poll_seconds: float = 30.0) -> None:
        """Blocking loop for the long-running daemon.

        Performs the startup catch-up first, then wakes periodically and runs
        whenever the interval has elapsed. Safe to kill at any point (NFR-4);
        progress is persisted per run.
        """
        self.startup()
        log.info("scheduler started; interval=%sh", self.config.run_interval_hours)
        while True:
            wait = self.seconds_until_next()
            if wait <= 0:
                self.run_once()
                continue
            time.sleep(min(wait, poll_seconds))
