"""Collection pipeline — one run, Telegram-only.

Straight line, no database:

    for each enabled source:
        fetch new items since its last success   (failures isolated per source)
    classify each item                           (recall-first)
    extract fields                               (title, remote, salary, …)
    keep those passing keyword + remote filter
    merge near-duplicates across sources
    drop any whose fingerprint is already in the database (already sent)
    send the survivors as one batched Telegram digest
    on a successful send, record their fingerprints in the state
    optionally auto-apply to the delivered ones   (jobradar/apply/, off by default)

A source that fails does not stop the others. A Tier B scraper that fails three
runs in a row is disabled in the state file and the owner is notified. A crash
mid-run loses nothing that matters: unsent postings simply aren't marked, so
the next run re-detects and sends them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import dedup, extraction
from .classifier import classify
from .collectors.base import CollectorError, FetchContext, HttpClient, SourceBlocked
from .collectors.registry import build_collector
from .config import Config
from .delivery import format_message
from .filters import PASS, Settings, evaluate
from .models import Posting
from .state import State, iso, parse_iso, utcnow

log = logging.getLogger("jobradar.pipeline")


@dataclass
class RunSummary:
    trigger: str
    started_at: datetime
    items_fetched: int = 0
    skipped_old: int = 0
    postings_detected: int = 0
    passed_filter: int = 0
    rejected_keyword: int = 0
    rejected_remote: int = 0
    rejected_region: int = 0
    rejected_salary: int = 0
    rejected_excluded: int = 0
    rejected_ai: int = 0
    duplicates_merged: int = 0
    already_sent_skipped: int = 0
    reposts_skipped: int = 0     # subset of already_sent_skipped: reworded reposts
    held_back: int = 0
    delivered: int = 0
    sent: list[Posting] = field(default_factory=list)
    delivered_postings: list[Posting] = field(default_factory=list)
    apply: Any = None   # jobradar.apply.engine.ApplyReport when auto-apply is on
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
        applier: Any | None = None,
    ):
        self.config = config
        self.state = state
        self.source_defs = source_defs
        self.transport = transport  # send-only Telegram transport, or None
        self.applier = applier      # jobradar.apply.engine.Applier, or None (off)
        self._sigs: dict[str, tuple[str | None, Any]] = {}  # posting.id -> (minhash, roles)

    # ------------------------------------------------------------------ run
    def run(self, trigger: str = "manual") -> RunSummary:
        now = utcnow()
        summary = RunSummary(trigger=trigger, started_at=now)
        self._sigs: dict[str, tuple[str | None, Any]] = {}  # posting.id -> (minhash, roles)

        raw_items: list[tuple[dict[str, Any], Any]] = []
        for sdef in self.source_defs:
            if not sdef.get("enabled", True) or self.state.is_disabled(sdef["name"]):
                continue
            raw_items.extend((sdef, item) for item in self._collect(sdef, now, summary))

        summary.items_fetched = len(raw_items)

        # Classify → extract → filter.
        kept: list[Posting] = []
        settings = Settings(keywords=self.config.keywords, remote_only=self.config.remote_only,
                            regions=self.config.regions, min_salary_usd=self.config.min_salary_usd,
                            require_salary=self.config.require_salary,
                            exclude_keywords=self.config.exclude_keywords,
                            send_unknown_location=self.config.send_unknown_location)
        max_age = self.config.max_posting_age_days
        age_cutoff = now - timedelta(days=max_age) if max_age and max_age > 0 else None
        for sdef, item in raw_items:
            ps = summary.per_source.setdefault(sdef["name"], {})
            try:
                posting = self._to_posting(sdef, item, now)
            except Exception as e:  # noqa: BLE001 - one odd post must not stop the run (NFR-6)
                ps["unreadable"] = ps.get("unreadable", 0) + 1
                log.warning("%s: couldn't read one post (%s: %s); skipped it", sdef["name"], type(e).__name__, e)
                continue
            if posting is None:
                ps["not_job"] = ps.get("not_job", 0) + 1  # classifier said "not a posting"
                continue
            # Skip postings older than the configured age limit (dated ones only;
            # undated posts can't be aged out and are kept).
            if age_cutoff and posting.posted_at and posting.posted_at < age_cutoff:
                summary.skipped_old += 1
                ps["old"] = ps.get("old", 0) + 1
                continue
            summary.postings_detected += 1
            try:
                result = evaluate(posting, settings)
            except Exception as e:  # noqa: BLE001
                ps["unreadable"] = ps.get("unreadable", 0) + 1
                log.warning("%s: couldn't filter one post (%s: %s); skipped it", sdef["name"], type(e).__name__, e)
                continue
            if result.decision != PASS:
                ps[f"rej_{result.stage}"] = ps.get(f"rej_{result.stage}", 0) + 1
                if result.stage == "keyword":
                    summary.rejected_keyword += 1
                elif result.stage == "remote":
                    summary.rejected_remote += 1
                elif result.stage == "region":
                    summary.rejected_region += 1
                elif result.stage == "salary":
                    summary.rejected_salary += 1
                elif result.stage == "excluded":
                    summary.rejected_excluded += 1
                continue
            posting.matched_keywords = result.matched_keywords
            posting.location_unknown = result.reason == "location_not_stated"
            ps["passed"] = ps.get("passed", 0) + 1
            kept.append(posting)
        summary.passed_filter = len(kept)

        # Merge near-duplicates within this run (cross-source), then drop those
        # already sent on a previous run.
        deduped = self._merge_duplicates(kept, summary)
        fresh = [p for p in deduped if not self._already_sent(p, summary)]

        # Cap sends per run so a first-run backlog doesn't flood the chat with
        # hundreds of messages. Keep the newest, hold the rest for later runs
        # (they're not marked sent, so they go out over the next few runs).
        cap = self.config.max_messages_per_run
        if cap and len(fresh) > cap:
            fresh.sort(key=lambda p: p.posted_at, reverse=True)  # newest first
            summary.held_back = len(fresh) - cap
            fresh = fresh[:cap]

        # One message per posting (sent separately, oldest first).
        fresh.sort(key=lambda p: p.posted_at)
        # Optional AI enrichment — only for the postings we're about to send, so
        # the API is called at most once per delivered posting. As well as
        # cleaning fields, the AI vetoes anything that isn't an employer hiring
        # (job-seekers, "for hire" freelancers, ads) that the rules let through.
        if self.config.use_ai and self._ai_key():
            surviving: list[Posting] = []
            for p in fresh:
                if self._enrich(p):
                    surviving.append(p)
                else:
                    summary.rejected_ai += 1
                    self._remember_sent(p)  # don't re-check next run
            fresh = surviving
        summary.sent = fresh
        summary.messages = [self._message(p) for p in fresh]

        delivered_ok = self._deliver(fresh, summary)

        # Advance each source's watermark ONLY once this run's postings are
        # safely delivered AND nothing was held back by the per-run cap. If a
        # send failed, no Telegram is configured, or a backlog remains, we hold
        # the watermarks so the unsent postings are re-fetched and retried next
        # run rather than skipped forever. The sent-fingerprint set prevents
        # anything already delivered from being sent twice.
        if delivered_ok and summary.held_back == 0:
            for name, meta in summary.per_source.items():
                if meta.get("status") == "ok":
                    self.state.record_success(name, meta.get("latest"))
        # Save before auto-apply: delivery bookkeeping must survive even if
        # applying (which can take a while) is interrupted.
        self.state.save()

        if self.applier is not None:
            try:
                summary.apply = self.applier.run(summary.delivered_postings, now)
                summary.alerts.extend(summary.apply.alerts)
            except Exception as e:  # noqa: BLE001 - applying must never break a run
                log.exception("auto-apply failed")
                summary.alerts.append(f"auto-apply failed: {e}")

        self.state.mark_run(now)
        self.state.save()
        return summary

    # -------------------------------------------------------------- collect
    def _collect(self, sdef: dict[str, Any], now: datetime, summary: RunSummary) -> list[Any]:
        name = sdef["name"]
        tier = sdef.get("tier", "A")
        interval = sdef.get("request_interval_s") or self.config.default_request_interval_seconds
        last = self.state.source_last_success(name)
        # The very first time a source runs, look back further so a new install
        # immediately surfaces a backlog of existing jobs.
        lookback = self.config.catchup_lookback_hours if last else self.config.first_run_lookback_hours
        floor = now - timedelta(hours=lookback)
        since = last or floor
        if since < floor:
            since = floor

        http = HttpClient(self.config.user_agent, request_interval_s=interval)
        cfg = {k: v for k, v in sdef.items()
               if k not in ("name", "type", "tier", "enabled", "request_interval_s")}
        # Make credentials from the config file available to collectors that
        # need them (Reddit, X). A source block can still override per source.
        for key in ("reddit_client_id", "reddit_client_secret", "x_bearer_token",
                    "telegram_bot_token"):
            cfg.setdefault(key, getattr(self.config, key, None))
        ctx = FetchContext(since=since, now=now, user_agent=self.config.user_agent,
                           request_interval_s=interval, config=cfg)

        ps = summary.per_source.setdefault(name, {"tier": tier})
        try:
            try:
                collector = build_collector(name=name, type_=sdef["type"], tier=tier, config=cfg, http=http)
            except KeyError as e:
                raise CollectorError(f"unknown source type {sdef.get('type')!r} — check the [[sources]] "
                                     "block in config.toml") from e
            try:
                items = list(collector.fetch(ctx))
            except CollectorError:
                raise
            except Exception as e:  # noqa: BLE001 - a broken source must never stop the run (SR-3)
                log.debug("source %s raised", name, exc_info=True)
                raise CollectorError(f"unexpected {type(e).__name__}: {e}") from e
            latest = since
            for it in items:
                if it.posted_at and it.posted_at.tzinfo is None:
                    it.posted_at = it.posted_at.replace(tzinfo=timezone.utc)  # feeds without a zone
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
            location=ext.location, is_remote=ext.is_remote, is_worldwide=ext.is_worldwide,
            salary=ext.salary, apply_url=ext.apply_url, source=item.source,
            source_tier=sdef.get("tier", "A"), source_url=item.url or "", origins=[item.source],
            content_hash=dedup.content_hash(item.raw_text), posted_at=posted, collected_at=now,
            source_type=sdef.get("type", ""),
        )

    def _merge_duplicates(self, postings: list[Posting], summary: RunSummary) -> list[Posting]:
        kept: list[Posting] = []
        for p in postings:
            match = None
            for k in kept:
                if dedup.is_duplicate(p.description, k.description, p.apply_url, k.apply_url,
                                      title_a=p.title, title_b=k.title):
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
        # Also accept the fingerprint older versions stored (before CJK-aware
        # dedup), so upgrading never re-sends what was already delivered. Safe to
        # remove once state files from before that change have aged out.
        legacy = dedup.legacy_content_hash(posting.description)
        if legacy and legacy != posting.content_hash and self.state.already_sent(legacy):
            self.state.mark_sent(posting.content_hash)  # migrate to the new fingerprint
            summary.already_sent_skipped += 1
            return True
        # A reworded repost of something sent within repost_window_days (next
        # month's HN thread, a cross-post with a new title). Its own signature is
        # remembered too, so a job that keeps being reposted with small edits
        # keeps being recognised as the edits accumulate.
        sig, roles = self._signature(posting)
        if sig and self.state.find_repost(sig, roles):
            self._remember_sent(posting)
            summary.already_sent_skipped += 1
            summary.reposts_skipped += 1
            return True
        return False

    def _signature(self, posting: Posting) -> tuple[str | None, Any]:
        """MinHash + role signature, computed once per posting per run (from the
        rule-based title, before AI enrichment can rename it)."""
        if posting.id not in self._sigs:
            sig = dedup.minhash(posting.description) if self.config.repost_window_days > 0 else None
            self._sigs[posting.id] = (sig, dedup.role_signature(posting.title))
        return self._sigs[posting.id]

    def _remember_sent(self, posting: Posting) -> None:
        """Record a posting as handled: exact fingerprint + near-dup signature."""
        self.state.mark_sent(posting.content_hash)
        sig, roles = self._signature(posting)
        if sig:
            self.state.remember_signature(posting.content_hash, sig, dedup.encode_roles(roles))

    # -------------------------------------------------------------- deliver
    def _deliver(self, postings: list[Posting], summary: RunSummary) -> bool:
        """Send one Telegram message per posting, oldest first.

        Each posting is marked sent the instant its own message goes out, so a
        failure part-way through never re-sends the ones already delivered. A
        short gap between messages keeps us under Telegram's per-chat flood
        limit. Returns True only if everything (that needed sending) went out.
        """
        if not postings:
            return True  # nothing new; safe to advance watermarks
        if not (self.transport and self.config.telegram_chat_id):
            log.info("no Telegram configured; %d posting(s) detected but not sent "
                     "(they will be sent once a token/chat is set)", len(postings))
            return False

        chat = self.config.telegram_chat_id
        for i, posting in enumerate(postings):
            try:
                self.transport.send_message(chat, self._message(posting))
                self._remember_sent(posting)  # mark per message
                summary.delivered += 1
                summary.delivered_postings.append(posting)
                self.state.record_job({
                    "fingerprint": posting.content_hash, "at": iso(utcnow()),
                    "title": posting.title[:300], "url": posting.source_url,
                    "source": ", ".join(posting.origins or [posting.source]),
                    "remote": posting.is_remote, "location": posting.location or "",
                    "salary": posting.salary.raw or "", "posted_at": iso(posting.posted_at),
                    "description": posting.description,  # full original text (FR-12)
                })
                ps = summary.per_source.setdefault(posting.source, {})
                ps["sent"] = ps.get("sent", 0) + 1
            except Exception as e:  # noqa: BLE001 - outage is retried, not fatal
                hint = _telegram_hint(str(e))
                log.warning("Telegram send failed after %d/%d; will retry the rest "
                            "next run: %s%s", i, len(postings), e, hint)
                summary.alerts.append(f"telegram send failed: {e}{hint}")
                return False
            if i + 1 < len(postings):
                self._pause(self.config.message_interval_seconds)
        return True

    def _message(self, posting: Posting) -> str:
        """The Telegram job card; if a post is too odd to format, a plain
        title + link is sent instead of failing the delivery."""
        try:
            return format_message(self._as_dict(posting))
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't format a job card (%s: %s); sending a plain one", type(e).__name__, e)
            from .delivery.digest import _esc

            return f"#hiring\n<b>{_esc(posting.title or '(untitled)')}</b>\n\n🔗 {_esc(posting.source_url)}"

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            import time

            time.sleep(seconds)

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
    def _ai_key(self) -> str | None:
        """The API key for the configured AI provider (or None if unset)."""
        if (self.config.ai_provider or "claude").lower() == "gemini":
            return self.config.gemini_api_key
        return self.config.anthropic_api_key

    def _enrich(self, p: Posting) -> bool:
        """Enrich a posting with the configured AI provider. Returns False to
        VETO the posting (the AI judged it not an employer job posting), True to
        keep it. On any AI failure the posting is kept unchanged (True)."""
        from . import ai

        result = ai.enrich(
            p.description, provider=self.config.ai_provider,
            api_key=self._ai_key() or "", model=self.config.ai_model,
            max_chars=self.config.ai_max_chars,
        )
        if result is None:
            return True  # AI unavailable — keep the rule-based result
        if not result.is_hiring:
            return False  # AI says this isn't an employer hiring — drop it
        if result.title:
            p.title = result.title
        if result.location:
            p.location = result.location
        if result.is_remote in ("remote", "hybrid", "onsite", "unknown"):
            p.is_remote = result.is_remote
        p.is_worldwide = p.is_worldwide or result.is_worldwide
        if result.salary and not p.salary.raw:
            p.salary.raw = result.salary
        if result.apply and not p.contact:
            p.contact = result.apply
        p.responsibilities = result.responsibilities
        p.requirements = result.requirements
        return True

    def _as_dict(self, p: Posting) -> dict[str, Any]:
        return {
            "title": p.title, "description": p.description, "contact": p.contact,
            "source": p.source, "source_tier": p.source_tier,
            "source_url": p.source_url, "origins": p.origins, "is_remote": p.is_remote,
            "location": p.location, "is_worldwide": p.is_worldwide,
            "location_unknown": p.location_unknown,
            "salary_raw": p.salary.raw, "salary_min": p.salary.min, "salary_max": p.salary.max,
            "salary_currency": p.salary.currency, "matched_keywords": p.matched_keywords,
            "responsibilities": p.responsibilities, "requirements": p.requirements,
        }


def _telegram_hint(err: str) -> str:
    """A short, actionable hint for common Telegram setup errors."""
    e = err.lower()
    if "not enough rights" in e or "need administrator" in e:
        return (" — the bot can't post in that chat. If it's a channel/group, add "
                "the bot as an admin with 'Post/Send Messages' rights; if it's your "
                "DM, set telegram_chat_id to your own user id.")
    if "chat not found" in e:
        return (" — telegram_chat_id is wrong or the bot has never met that chat. "
                "Press Start in your DM with the bot, or re-check the id.")
    if "bot was blocked" in e or "blocked by the user" in e:
        return " — you blocked the bot in Telegram; unblock it and press Start."
    return ""
