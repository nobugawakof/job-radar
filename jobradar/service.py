"""Application wiring — Telegram-only build."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

from .config import Config, build_sources
from .delivery import TelegramTransport
from .pipeline import Pipeline, RunSummary
from .state import State, iso, parse_iso, utcnow

log = logging.getLogger("jobradar.service")


class Service:
    def __init__(self, config: Config, config_path: str | None = None):
        self.config = config
        self.config_path = config_path
        # Resolve a relative state path next to the config file, so a packaged
        # .exe keeps its state beside config.toml rather than in a random cwd.
        state_path = Path(config.state_path)
        if not state_path.is_absolute() and config_path:
            state_path = Path(config_path).resolve().parent / state_path
        self.state = State(str(state_path), max_sent=config.max_sent_remembered)
        self.source_defs = build_sources(config, config_path)
        self.transport = TelegramTransport(config.telegram_bot_token) if config.telegram_bot_token else None
        self.pipeline = Pipeline(config, self.state, self.source_defs, transport=self.transport)

    def run(self, trigger: str = "manual") -> RunSummary:
        return self.pipeline.run(trigger=trigger)

    def run_forever(self, *, poll_seconds: float = 60.0) -> None:
        """Interval loop, hardened for an always-on desktop.

        Two things this must survive on a personal PC:

        * **A single run failing.** Every run is wrapped so an unexpected error
          is logged and the daemon keeps going — one bad run must never stop all
          future runs (that would look like "it only worked the first time").
        * **The machine sleeping.** Scheduling is by wall clock against the
          persisted ``last_run`` timestamp, not a monotonic countdown. If the PC
          sleeps past the next due time, the run simply fires on wake and catches
          up; a monotonic timer can stall while suspended and never fire.
        """
        interval = timedelta(hours=self.config.run_interval_hours)
        log.info("job-radar started; interval=%sh, sources=%d — leave this window open.",
                 self.config.run_interval_hours, len(self.source_defs))
        # Always run once on startup (covers any downtime while it was closed).
        self._safe_run("startup")
        announced = None
        while True:
            last = parse_iso(self.state.last_run) or utcnow()
            due = last + interval
            now = utcnow()
            if now >= due:
                self._safe_run("scheduled")
                announced = None  # recompute/announce the next due time
                continue
            if announced != due:
                log.info("next run due %s (in ~%.1fh)", iso(due),
                         (due - now).total_seconds() / 3600)
                announced = due
            time.sleep(min(poll_seconds, max(1.0, (due - now).total_seconds())))

    def _safe_run(self, trigger: str) -> RunSummary | None:
        """Run once, never letting an exception escape and kill the daemon."""
        try:
            return self._run_logged(trigger)
        except Exception:  # noqa: BLE001 - a bad run must not stop the loop
            log.exception("run(%s) failed; the daemon will keep running and "
                          "retry at the next interval", trigger)
            # Advance the watermark so a persistent failure doesn't hot-loop;
            # the next attempt waits a full interval like a normal run.
            self.state.mark_run(utcnow())
            try:
                self.state.save()
            except Exception:  # noqa: BLE001
                log.exception("could not save state after a failed run")
            return None

    def _run_logged(self, trigger: str) -> RunSummary:
        summary = self.run(trigger)
        log.info("run(%s): %d fetched, %d postings, %d merged, %d already-sent, %d delivered",
                 trigger, summary.items_fetched, summary.postings_detected,
                 summary.duplicates_merged, summary.already_sent_skipped, summary.delivered)
        for a in summary.alerts:
            log.warning("alert: %s", a)
        return summary
