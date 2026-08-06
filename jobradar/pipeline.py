"""Collection pipeline — one run across all enabled sources.

Responsibilities, mapped to requirements:

* Query each enabled source for content since its last success (FR-2/FR-3).
* Store every raw item before parsing (FR-6). Raw items are durable the moment
  they are fetched, so a crash mid-processing loses nothing: the next run
  reprocesses anything still marked unprocessed (NFR-4, AC-9).
* Classify (FR-7/8), extract (FR-10-12), deduplicate (FR-27-29), then fan the
  survivor out to each member through their own filter (Section 4.4), so two
  members get different digests (AC-6).
* Isolate failures: one source failing never halts the run (SR-3); one
  malformed post never aborts its batch (NFR-6).
* Auto-disable a Tier B source after three consecutive failures and alert the
  owner (SR-4), while Tier A failures are surfaced as defects but retried.

Fetching (network) happens outside any DB transaction; each raw item is stored
in its own atomic statement; each item's processing is wrapped in a
transaction. This ordering is what gives the clean crash-resume story.
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
from .db import parse_iso, utcnow
from .filters import PASS, REVIEW, evaluate
from .models import (
    Posting,
    STATUS_NEW,
    STATUS_PENDING_REVIEW,
    RawItem,
)
from .repos import Store

log = logging.getLogger("jobradar.pipeline")

# How far back to look for duplicates when merging (FR-27/28).
DEDUP_WINDOW_DAYS = 30


@dataclass
class RunSummary:
    run_id: int
    trigger: str
    window_start: datetime | None
    window_end: datetime
    items_fetched: int = 0
    postings_detected: int = 0
    duplicates_merged: int = 0
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        store: Store,
        config: Config,
        *,
        notifier: Callable[[str, str], None] | None = None,
    ):
        self.store = store
        self.config = config
        # notifier(owner_chat_or_name, message) — used for SR-4 owner alerts.
        self.notifier = notifier

    # ------------------------------------------------------------------ run
    def run(self, trigger: str = "scheduled", since_override: datetime | None = None) -> RunSummary:
        now = utcnow()
        window_start = since_override
        run_id = self.store.start_run(trigger, window_start, now)
        summary = RunSummary(run_id=run_id, trigger=trigger, window_start=window_start, window_end=now)

        http_prototype = HttpClient(self.config.user_agent)

        for source in self.store.list_sources(enabled_only=True):
            self._collect_source(source, now, since_override, run_id, summary, http_prototype)

        # Process everything not yet processed (includes crash leftovers).
        detected = self._process_pending(now, summary)
        for src, count in detected.items():
            ps = summary.per_source.setdefault(src, {})
            ps["postings_detected"] = count
            self.store.record_run_source(
                run_id, src,
                items_fetched=ps.get("items_fetched", 0),
                postings_detected=count,
                status=ps.get("status", "ok"),
                error=ps.get("error"),
            )
            summary.postings_detected += count

        self.store.finish_run(run_id)
        return summary

    # -------------------------------------------------------------- collect
    def _collect_source(
        self,
        source: dict[str, Any],
        now: datetime,
        since_override: datetime | None,
        run_id: int,
        summary: RunSummary,
        http_prototype: HttpClient,
    ) -> None:
        name = source["name"]
        interval = source["request_interval_s"] or self.config.default_request_interval_seconds
        # Per-source window: since last success, but never further back than the
        # catch-up lookback cap (FR-5). A manual override wins.
        floor = now - timedelta(hours=self.config.catchup_lookback_hours)
        since = since_override or parse_iso(source["last_success_at"]) or floor
        if since < floor:
            since = floor

        http = HttpClient(self.config.user_agent, request_interval_s=interval)
        ctx = FetchContext(
            since=since, now=now, user_agent=self.config.user_agent,
            request_interval_s=interval, config=source["config"],
        )

        ps = summary.per_source.setdefault(name, {})
        try:
            collector = build_collector(
                name=name, type_=source["type"], tier=source["tier"],
                config=source["config"], http=http,
            )
            fetched = 0
            latest_at = since
            for item in collector.fetch(ctx):
                # Store raw before parsing (FR-6); idempotent on (source, ext_id).
                self.store.store_raw_item(item)
                fetched += 1
                if item.posted_at and item.posted_at > latest_at:
                    latest_at = item.posted_at
            self.store.record_source_success(name, latest_at)
            ps.update(items_fetched=fetched, status="ok")
            summary.items_fetched += fetched
            self.store.record_run_source(run_id, name, items_fetched=fetched, status="ok")

        except SourceBlocked as e:
            # NFR-18: block is permanent until manual re-enable.
            self.store.block_source(name)
            msg = f"Source '{name}' reported a block and was disabled: {e}"
            ps.update(items_fetched=0, status="error", error=str(e))
            self.store.record_run_source(run_id, name, status="error", error=str(e))
            self._alert(summary, msg)

        except CollectorError as e:
            # SR-3: other sources continue. SR-4: Tier B auto-disable after N.
            failures = self.store.record_source_failure(name)
            ps.update(items_fetched=0, status="error", error=str(e))
            self.store.record_run_source(run_id, name, status="error", error=str(e))
            if source["tier"] == "B" and failures >= self.config.tier_b_failure_threshold:
                self.store.set_source_enabled(name, False)
                self._alert(
                    summary,
                    f"Tier B source '{name}' auto-disabled after {failures} consecutive "
                    f"failures (last error: {e})",
                )
            else:
                log.warning("source %s failed (%s/%s): %s",
                            name, failures, self.config.tier_b_failure_threshold, e)

    # -------------------------------------------------------------- process
    def _process_pending(self, now: datetime, summary: RunSummary) -> dict[str, int]:
        """Classify/extract/dedup/fan-out every unprocessed raw item.

        Picks up items from this run *and* any left unprocessed by a crash.
        """
        rows = self.store.db.query(
            "SELECT * FROM raw_items WHERE processed=0 ORDER BY id"
        )
        detected: dict[str, int] = {}
        for row in rows:
            try:
                is_posting = self._process_one(row, now, summary)
            except Exception as e:  # noqa: BLE001 - one bad post never aborts the batch (NFR-6)
                log.exception("failed to process raw item %s: %s", row["id"], e)
                # Mark processed so a poison item doesn't wedge every future run.
                self.store.mark_raw_processed(row["id"], is_posting=False)
                continue
            if is_posting:
                detected[row["source"]] = detected.get(row["source"], 0) + 1
        return detected

    def _process_one(self, row: Any, now: datetime, summary: RunSummary) -> bool:
        source = self.store.get_source(row["source"])
        tier = source["tier"] if source else "A"
        text = row["raw_text"]

        # Recall-first classification (FR-7/8). A dedicated jobs feed can set a
        # prior in its config to keep short posts.
        prior = float((source or {}).get("config", {}).get("classifier_prior", 0.0)) if source else 0.0
        result = classify(text, source_prior=prior)
        if not result.is_posting:
            self.store.mark_raw_processed(row["id"], is_posting=False)
            return False

        ext = extraction.extract(
            text,
            given_title=row["title_hint"],
            given_location=row["location_hint"],
        )
        posted_at = parse_iso(row["posted_at"]) or now  # Section 5: fall back to collection time
        posting = Posting(
            title=ext.title,
            description=ext.description,  # FR-12: unmodified
            contact=ext.contact,
            location=ext.location,
            is_remote=ext.is_remote,
            hiring_countries=ext.hiring_countries,
            is_worldwide=ext.is_worldwide,
            salary=ext.salary,
            apply_url=ext.apply_url,
            source=row["source"],
            source_tier=tier,
            source_url=row["url"] or "",
            origins=[row["source"]],
            content_hash=dedup.content_hash(text),
            posted_at=posted_at,
            collected_at=now,
        )

        with self.store.db.transaction():
            canonical = self._find_duplicate(posting)
            if canonical is not None:
                # FR-27/29: deliver once; keep both origins on the survivor.
                posting.duplicate_of = canonical["id"]
                self.store.insert_posting(posting)
                self.store.add_origin(canonical["id"], posting.source)
                self.store.mark_raw_processed(row["id"], is_posting=True)
                summary.duplicates_merged += 1
                return True

            self.store.insert_posting(posting)
            self._fanout(posting)
            self.store.mark_raw_processed(row["id"], is_posting=True)
        return True

    def _find_duplicate(self, posting: Posting) -> dict[str, Any] | None:
        since = posting.collected_at - timedelta(days=DEDUP_WINDOW_DAYS)
        for existing in self.store.recent_postings(since):
            if dedup.is_duplicate(
                posting.description, existing["description"],
                posting.apply_url, existing.get("apply_url"),
            ):
                return existing
        return None

    def _fanout(self, posting: Posting) -> None:
        """Evaluate the posting for every active member (AC-6, NFR-8)."""
        for user in self.store.list_users():
            settings = self.store.user_settings(user["id"])
            if settings is None:
                continue
            decision = evaluate(posting, settings)
            if decision.decision == PASS:
                self.store.upsert_user_posting(
                    user["id"], posting.id, STATUS_NEW, decision.matched_keywords
                )
            elif decision.decision == REVIEW:
                # FR-20/21: undetermined geography → per-user review queue.
                self.store.upsert_user_posting(
                    user["id"], posting.id, STATUS_PENDING_REVIEW, decision.matched_keywords
                )
            # REJECT → nothing stored for this user.

    # --------------------------------------------------------------- alerts
    def _alert(self, summary: RunSummary, message: str) -> None:
        log.warning(message)
        summary.alerts.append(message)
        # Deliver to the owner if we can (SR-4).
        for user in self.store.list_users():
            if user["is_owner"]:
                self.store.enqueue_delivery(user["id"], None, {"type": "alert", "text": message})
                if self.notifier and user["telegram_chat_id"]:
                    try:
                        self.notifier(user["telegram_chat_id"], message)
                    except Exception:  # noqa: BLE001 - alerting must not crash a run
                        log.exception("owner alert delivery failed")
                break

    # ---------------------------------------------------- maintenance/purge
    def housekeeping(self, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        return {
            "expired_reviews": self.store.expire_stale_reviews(self.config.review_expiry_days, now),
            "purged_non_postings": self.store.purge_old_non_postings(
                self.config.non_posting_retention_days, now
            ),
            "purged_postings": self.store.purge_old_postings(
                self.config.posting_retention_days, now
            ),
        }
