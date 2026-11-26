"""Application wiring — Telegram-only build."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from .config import Config, build_sources
from .delivery import TelegramTransport
from .delivery.inbox import inbox_for
from .pipeline import Pipeline, RunSummary
from .state import State, iso, parse_iso, utcnow

log = logging.getLogger("jobradar.service")


class AlreadyRunning(RuntimeError):
    """Another program sharing the database is in the middle of a check."""


class Service:
    def __init__(self, config: Config, config_path: str | None = None, *,
                 state: State | None = None):
        self.config = config
        self.config_path = config_path
        # Resolve a relative state path next to the config file, so a packaged
        # .exe keeps its state beside config.toml rather than in a random cwd.
        self.base_dir = Path(config_path).resolve().parent if config_path else Path.cwd()
        state_path = Path(config.state_path)
        if not state_path.is_absolute():
            state_path = self.base_dir / state_path
        # The state lives in SQLite. An older config may still name the JSON
        # file ("jobradar-state.json"); use a .db beside it and import the JSON once.
        legacy_json = state_path if state_path.suffix.lower() == ".json" else \
            state_path.with_name("jobradar-state.json")
        if state_path.suffix.lower() == ".json":
            state_path = state_path.with_suffix(".db")
        if state is not None and Path(state.path).resolve() == state_path.resolve():
            # The dashboard rebuilds the Service after a settings change; it must
            # keep sharing one State, or two copies would overwrite each other.
            state.max_sent = config.max_sent_remembered
            state.repost_window_days = config.repost_window_days
            self.state = state
        else:
            self.state = State(str(state_path), max_sent=config.max_sent_remembered,
                               repost_window_days=config.repost_window_days,
                               legacy_json=legacy_json)
        self.source_defs = build_sources(config, config_path)
        # Resolve the Telegram account session next to the config, for every user of it.
        session = Path(config.telegram_session_path).expanduser()
        if not session.is_absolute():
            session = self.base_dir / session
        self.config = config = replace(config, telegram_session_path=str(session))
        token = config.telegram_bot_token
        has_bot = bool(token) and not str(token).upper().startswith("PASTE")
        self.transport = TelegramTransport(token) if has_bot else None
        self.inbox = inbox_for(token, self.state) if has_bot else None
        self.applier = self._build_applier()
        self.pipeline = Pipeline(config, self.state, self.source_defs, transport=self.transport,
                                 applier=self.applier, inbox=self.inbox)
        if self.inbox is not None:
            self.inbox.on_button("rv:", self._review_button)

    # ------------------------------------------------------- conversations
    def sync_conversations(self, *, lock_timeout: float = 15.0) -> dict:
        """Read new messages in every conversation with a representative from
        your Telegram account, and notify you about new replies."""
        from .apply.conversations import sync_telegram

        try:
            return sync_telegram(self.config, self.state, notify=self.notify_owner, lock_timeout=lock_timeout)
        except Exception as e:  # noqa: BLE001 - never break a check over this
            log.warning("couldn't read your Telegram conversations: %s", e)
            return {"synced": 0, "new_replies": 0, "error": str(e)}

    # ------------------------------------------------------------ review
    def review_decide(self, item_id: int, approve: bool, via: str) -> str:
        """Approve (send it to me) or reject a job waiting in the review queue.
        Safe against double presses and deciding in two places at once."""
        from .models import posting_from_dict

        item = self.state.review_get(item_id)
        if item is None:
            return "That job is no longer in the queue."
        if not self.state.review_resolve(item_id, "approved" if approve else "rejected", via):
            return "Already handled."
        if not approve:
            self.state.mark_sent(item["fingerprint"])  # never asked again
            return "Skipped."
        posting = posting_from_dict(item["posting"])
        posting.needs_review, posting.location_unknown = False, True
        try:
            self.pipeline.deliver_one(posting)
        except Exception as e:  # noqa: BLE001 - keep it in the queue
            self.state.review_reopen(item_id)
            log.warning("couldn't send an approved job: %s", e)
            return "Couldn't send it right now — try again in a moment."
        if self.applier is not None:
            self.applier.enqueue([posting], utcnow())  # applied at the next check
        return "Sent ✅"

    def _review_button(self, cq: dict) -> str:
        from .delivery.review import without_item

        try:
            _, raw_id, choice = (cq.get("data") or "").split(":")
            item_id = int(raw_id)
        except ValueError:
            return "Unknown button."
        note = self.review_decide(item_id, choice == "y", "telegram")
        msg = cq.get("message") or {}
        if msg.get("message_id") and self.inbox is not None:
            try:
                self.inbox.call("editMessageReplyMarkup", {
                    "chat_id": (msg.get("chat") or {}).get("id"), "message_id": msg["message_id"],
                    "reply_markup": without_item(msg.get("reply_markup"), item_id)})
            except Exception:  # noqa: BLE001 - the decision is saved either way
                pass
        return note

    def notify_owner(self, text: str) -> None:
        """Send a message to your own bot chat (used by auto-apply)."""
        if self.transport and self.config.telegram_chat_id:
            self.transport.send_message(self.config.telegram_chat_id, text)

    def _build_applier(self):
        mode = (self.config.apply_mode or "off").strip().lower()
        if mode == "off":
            return None
        if mode not in ("dry_run", "auto"):
            log.warning("unknown apply_mode %r (use off / dry_run / auto); auto-apply is off", mode)
            return None
        from .apply.engine import build_applier

        return build_applier(self.config, self.state, base_dir=self.base_dir,
                             notify=self.notify_owner)

    def run(self, trigger: str = "manual") -> RunSummary:
        """One check. Only one check runs at a time across programs sharing
        the database (raises :class:`AlreadyRunning` otherwise), and its summary
        is stored in the run history."""
        owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        if not self.state.acquire_run_lock(owner):
            holder = self.state.run_lock_holder() or {}
            raise AlreadyRunning(f"another Job Radar (process {holder.get('pid', '?')}) is checking "
                                 f"right now, started {holder.get('at', '?')[:16]}Z")
        try:
            summary = self.pipeline.run(trigger=trigger)
            if self.state.conversations("telegram"):
                self.sync_conversations(lock_timeout=60)  # replies from representatives
            try:
                self.state.record_run(summary.to_dict())
            except Exception:  # noqa: BLE001 - history is a nice-to-have
                log.exception("could not store the run summary")
            return summary
        finally:
            self.state.release_run_lock(owner)

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
        if self.inbox is not None:
            self.inbox.start()  # buttons (review queue) and channel posts, in the background
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
                if parse_iso(self.state.last_run) == parse_iso(iso(last)):
                    time.sleep(poll_seconds)  # skipped (another program was checking)
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
        except AlreadyRunning as e:
            log.info("skipped this check: %s", e)
            return None
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
