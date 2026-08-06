"""Application wiring.

Constructs the object graph — store, pipeline, delivery, scheduler, web — from
a :class:`~jobradar.config.Config`. This is the one place that knows how the
pieces fit together; everything else stays decoupled and unit-testable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import geo
from .config import Config, source_definitions
from .db import Database
from .delivery.telegram import DeliveryService, TelegramBot, TelegramTransport
from .pipeline import Pipeline, RunSummary
from .repos import Store
from .scheduler import Scheduler

log = logging.getLogger("jobradar.service")


class Service:
    def __init__(self, config: Config, config_path: str | None = None):
        self.config = config
        self.config_path = config_path
        self.db = Database(config.db_path)
        self.store = Store(self.db)
        self._transport: TelegramTransport | None = None
        if config.telegram_bot_token:
            self._transport = TelegramTransport(config.telegram_bot_token)
        self.pipeline = Pipeline(self.store, config, notifier=self._notify)
        self.scheduler = Scheduler(self.store, config, self.pipeline)

    # ---- notifications ----------------------------------------------------
    def _notify(self, chat_id: str, text: str) -> None:
        if self._transport:
            self._transport.send_message(chat_id, text)

    # ---- setup ------------------------------------------------------------
    def init(self, owner_name: str = "owner") -> str:
        """Create the owner (if absent) and seed sources from config (SR-1)."""
        owners = [u for u in self.store.list_users() if u["is_owner"]]
        if owners:
            owner_id = owners[0]["id"]
        else:
            owner_id = self.store.create_user(
                owner_name, is_owner=True,
                keywords=self.config.default_keywords,
                eligible_countries=self.config.default_eligible_countries,
                remote_only=self.config.default_remote_only,
            )
            self.store.issue_dashboard_token(owner_id)
            self.store.issue_link_code(owner_id)
        self.seed_sources()
        return owner_id

    def seed_sources(self) -> int:
        defs = source_definitions(self.config_path)
        for d in defs:
            self.store.upsert_source(
                d["name"], d["type"], d.get("tier", "A"),
                enabled=d.get("enabled", True),
                config={k: v for k, v in d.items()
                        if k not in ("name", "type", "tier", "enabled", "request_interval_s")},
                request_interval_s=d.get("request_interval_s"),
            )
        return len(defs)

    def add_member(self, name: str, *, countries: list[str] | None = None) -> dict[str, Any]:
        uid = self.store.create_user(
            name,
            keywords=self.config.default_keywords,
            eligible_countries=geo.normalise_country_list(countries or [])
            or self.config.default_eligible_countries,
            remote_only=self.config.default_remote_only,
        )
        token = self.store.issue_dashboard_token(uid)
        code = self.store.issue_link_code(uid)
        return {"id": uid, "name": name, "dashboard_token": token, "telegram_link_code": code}

    # ---- run + deliver ----------------------------------------------------
    def run_and_deliver(self, trigger: str = "manual") -> RunSummary:
        summary = self.scheduler.run_once(trigger=trigger)
        self.deliver()
        return summary

    def deliver(self) -> dict[str, int]:
        if not self._transport:
            log.info("no Telegram token configured; skipping push delivery "
                     "(postings remain available in the dashboard)")
            return {"sent": 0, "failed": 0, "queued": 0}
        svc = DeliveryService(self.store, self._transport)
        queued = svc.send_run_digests(None)
        result = svc.deliver_pending()
        result["queued"] = queued
        return result

    def poll_bot(self) -> int:
        if not self._transport:
            return 0
        bot = TelegramBot(self.store, self._transport)
        return bot.poll_once()

    # ---- web --------------------------------------------------------------
    def build_web_context(self):
        from .web.app import AppContext

        return AppContext(store=self.store, config=self.config,
                          trigger_run=lambda: self.run_and_deliver("manual"))

    def close(self) -> None:
        self.db.close()
