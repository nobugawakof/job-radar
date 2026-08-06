"""Collection pipeline — one run, Telegram-only.

Straight line, no database:

    for each enabled source:
        fetch new items since its last success   (failures isolated per source)
    classify each item                           (recall-first)
    extract fields                               (title, remote, salary, …)
    keep those passing keyword + remote filter
    merge near-duplicates across sources
    drop any whose fingerprint is already in the JSON state (already sent)
    send the survivors as one batched Telegram digest
    on a successful send, record their fingerprints in the state

A source that fails does not stop the others. A Tier B scraper that fails three
runs in a row is disabled in the state file and the owner is notified. A crash
mid-run loses nothing that matters: unsent postings simply aren't marked, so
the next run re-detects and sends them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import dedup, extraction
from .classifier import classify
from .collectors.base import CollectorError, FetchContext, HttpClient, SourceBlocked
from .collectors.registry import build_collector
from .config import Config
from .delivery import build_digest
from .filters import PASS, Settings, evaluate
from .models import Posting
from .state import State, parse_iso, utcnow

log = logging.getLogger("jobradar.pipeline")


@dataclass
class RunSummary:
    trigger: str
    started_at: datetime
    items_fetched: int = 0
    postings_detected: int = 0
    duplicates_merged: int = 0
    already_sent_skipped: int = 0
    sent: list[Posting] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        config: Config,
        state: State,
        source_defs: list[dict[str, Any]],
        *,
        transport: Any | None = None,
    ):
        self.config = config
        self.state = state
        self.source_defs = source_defs
        self.transport = transport  # send-only Telegram transport, or None

    # ------------------------------------------------------------------ run
    def run(self, trigger: str = "manual") -> RunSummary:
        now = utcnow()
        summary = RunSummary(trigger=trigger, started_at=now)

        raw_items: list[tuple[dict[str, Any], Any]] = []
        for sdef in self.source_defs:
            if not sdef.get("enabled", True) or self.state.is_disabled(sdef["name"]):
                continue
            raw_items.extend((sdef, item) for item in self._collect(sdef, now, summary))

        summary.items_fetched = len(raw_items)

        # Classify → extract → filter.
        kept: list[Posting] = []
        settings = Settings(keywords=self.config.keywords, remote_only=self.config.remote_only)
        for sdef, item in raw_items:
            posting = self._to_posting(sdef, item, now)
            if posting is None:
                continue
            summary.postings_detected += 1
            result = evaluate(posting, settings)
            if result.decision != PASS:
                continue
            posting.matched_keywords = result.matched_keywords
            kept.append(posting)

        # Merge near-duplicates within this run (cross-source), then drop those
        # already sent on a previous run.
        deduped = self._merge_duplicates(kept, summary)
        fresh = [p for p in deduped if not self._already_sent(p, summary)]

        summary.sent = fresh
        summary.messages = build_digest([self._as_dict(p) for p in fresh])

        delivered_ok = self._deliver(summary)

        # Advance each source's watermark ONLY once this run's postings are
        # safely delivered. If a send failed (or no Telegram is configured yet),
        # we hold the watermarks so the unsent postings are re-fetched and
        # retried next run rather than skipped forever. The sent-fingerprint set
        # prevents anything already delivered from being sent twice.
        if delivered_ok:
            for name, meta in summary.per_source.items():
                if meta.get("status") == "ok":
                    self.state.record_success(name, meta.get("latest"))

        self.state.mark_run(now)
        self.state.save()
        return summary

    # -------------------------------------------------------------- collect
    def _collect(self, sdef: dict[str, Any], now: datetime, summary: RunSummary) -> list[Any]:
        name = sdef["name"]
        tier = sdef.get("tier", "A")
        interval = sdef.get("request_interval_s") or self.config.default_request_interval_seconds
        floor = now - timedelta(hours=self.config.catchup_lookback_hours)
        since = self.state.source_last_success(name) or floor
        if since < floor:
            since = floor

        http = HttpClient(self.config.user_agent, request_interval_s=interval)
        cfg = {k: v for k, v in sdef.items()
               if k not in ("name", "type", "tier", "enabled", "request_interval_s")}
        ctx = FetchContext(since=since, now=now, user_agent=self.config.user_agent,
                           request_interval_s=interval, config=cfg)

        ps = summary.per_source.setdefault(name, {"tier": tier})
        try:
            collector = build_collector(name=name, type_=sdef["type"], tier=tier, config=cfg, http=http)
            items = list(collector.fetch(ctx))
            latest = since
            for it in items:
                if it.posted_at and it.posted_at > latest:
                    latest = it.posted_at
            # Watermark is committed later, only if delivery succeeds (see run()).
            ps.update(items=len(items), status="ok", latest=latest)
            return items
        except SourceBlocked as e:
            self.state.disable_source(name)
            ps.update(items=0, status="blocked", error=str(e))
            self._alert(summary, f"Source '{name}' reported a block and was disabled: {e}")
            return []
        except CollectorError as e:
            failures = self.state.record_failure(name)
            ps.update(items=0, status="error", error=str(e))
            if tier == "B" and failures >= self.config.tier_b_failure_threshold:
                self.state.disable_source(name)
                self._alert(summary, f"Tier B source '{name}' auto-disabled after "
                                     f"{failures} consecutive failures (last error: {e})")
            else:
                log.warning("source %s failed (%s/%s): %s",
                            name, failures, self.config.tier_b_failure_threshold, e)
            return []

    # ----------------------------------------------------- classify/extract
    def _to_posting(self, sdef: dict[str, Any], item: Any, now: datetime) -> Posting | None:
        prior = float(sdef.get("classifier_prior", 0.0))
        if not classify(item.raw_text, source_prior=prior).is_posting:
            return None
        ext = extraction.extract(item.raw_text, given_title=item.title_hint,
                                 given_location=item.location_hint)
        posted = item.posted_at or now
        return Posting(
            title=ext.title, description=ext.description, contact=ext.contact,
            location=ext.location, is_remote=ext.is_remote, salary=ext.salary,
            apply_url=ext.apply_url, source=item.source, source_tier=sdef.get("tier", "A"),
            source_url=item.url or "", origins=[item.source],
            content_hash=dedup.content_hash(item.raw_text), posted_at=posted, collected_at=now,
        )

    def _merge_duplicates(self, postings: list[Posting], summary: RunSummary) -> list[Posting]:
        kept: list[Posting] = []
        for p in postings:
            match = None
            for k in kept:
                if dedup.is_duplicate(p.description, k.description, p.apply_url, k.apply_url):
                    match = k
                    break
            if match is None:
                kept.append(p)
            else:
                for o in p.origins:
                    if o not in match.origins:
                        match.origins.append(o)
                summary.duplicates_merged += 1
        return kept

    def _already_sent(self, posting: Posting, summary: RunSummary) -> bool:
        if self.state.already_sent(posting.content_hash):
            summary.already_sent_skipped += 1
            return True
        return False

    # -------------------------------------------------------------- deliver
    def _deliver(self, summary: RunSummary) -> bool:
        """Send the digest. Returns True when nothing needed sending or the
        whole digest went out; False when postings are left undelivered (no
        Telegram configured, or a send failed) so the caller holds the source
        watermarks and retries next run."""
        if not summary.messages:
            return True  # nothing new; safe to advance watermarks
        if not (self.transport and self.config.telegram_chat_id):
            log.info("no Telegram configured; %d posting(s) detected but not sent "
                     "(they will be sent once a token/chat is set)", len(summary.sent))
            return False
        try:
            for msg in summary.messages:
                self.transport.send_message(self.config.telegram_chat_id, msg)
            # Mark as sent only after the whole digest goes out (retry on failure).
            for p in summary.sent:
                self.state.mark_sent(p.content_hash)
            return True
        except Exception as e:  # noqa: BLE001 - outage is retried, not fatal
            log.warning("Telegram send failed; will retry next run: %s", e)
            summary.alerts.append(f"telegram send failed: {e}")
            return False

    # --------------------------------------------------------------- alerts
    def _alert(self, summary: RunSummary, message: str) -> None:
        log.warning(message)
        summary.alerts.append(message)
        if (self.config.notify_owner_on_source_disable and self.transport
                and self.config.telegram_chat_id):
            try:
                self.transport.send_message(self.config.telegram_chat_id, f"⚠️ {message}")
            except Exception:  # noqa: BLE001
                log.exception("owner alert send failed")

    # --------------------------------------------------------------- format
    def _as_dict(self, p: Posting) -> dict[str, Any]:
        return {
            "title": p.title, "source": p.source, "source_tier": p.source_tier,
            "source_url": p.source_url, "origins": p.origins, "is_remote": p.is_remote,
            "salary_raw": p.salary.raw, "salary_min": p.salary.min, "salary_max": p.salary.max,
            "salary_currency": p.salary.currency, "matched_keywords": p.matched_keywords,
        }
