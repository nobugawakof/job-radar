"""Application wiring — Telegram-only build."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config, source_definitions
from .delivery import TelegramTransport
from .pipeline import Pipeline, RunSummary
from .state import State

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
        self.source_defs = source_definitions(config_path)
        self.transport = TelegramTransport(config.telegram_bot_token) if config.telegram_bot_token else None
        self.pipeline = Pipeline(config, self.state, self.source_defs, transport=self.transport)

    def run(self, trigger: str = "manual") -> RunSummary:
        return self.pipeline.run(trigger=trigger)

    def run_forever(self, *, poll_seconds: float = 30.0) -> None:
        """Simple interval loop. Safe to kill at any point — state is saved per
        run and written atomically."""
        interval = self.config.run_interval_hours * 3600
        log.info("job-radar started; interval=%sh, sources=%d",
                 self.config.run_interval_hours, len(self.source_defs))
        # Run once immediately on startup (covers time the laptop was asleep).
        self._run_logged("startup")
        next_at = time.monotonic() + interval
        while True:
            if time.monotonic() >= next_at:
                self._run_logged("scheduled")
                next_at = time.monotonic() + interval
            time.sleep(min(poll_seconds, interval))

    def _run_logged(self, trigger: str) -> RunSummary:
        summary = self.run(trigger)
        log.info("run(%s): %d fetched, %d postings, %d merged, %d already-sent, %d sent",
                 trigger, summary.items_fetched, summary.postings_detected,
                 summary.duplicates_merged, summary.already_sent_skipped, len(summary.sent))
        for a in summary.alerts:
            log.warning("alert: %s", a)
        return summary
