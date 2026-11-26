"""Auto-apply engine.

Runs after a pipeline run has delivered its postings to you:

1. **Enqueue.** Each newly delivered posting that names an application contact
   (Telegram handle or email, see :mod:`.contacts`) and passes the apply
   gates — ``apply_exclude_keywords``, ``apply_keywords``, ``apply_max_age_days``
   — is put in a persistent queue in the state file. Nothing is lost when the
   daily cap is reached or a channel is paused; it waits for the next run.
2. **Process** the queue, newest first, until the rolling-24h cap is used up.
   For each entry: skip contacts messaged within ``apply_contact_cooldown_days``,
   draft the application (AI fit score + tailored message, or your template),
   drop it if the fit score is below ``apply_min_score``, then send it over the
   first channel in ``apply_channels`` that works.

``apply_mode``:

* ``"dry_run"`` — everything above happens, including the AI draft, but instead
  of contacting anyone the draft is shown to you in your bot chat. Use this to
  tune the gates and check the messages before going live.
* ``"auto"`` — the application really goes out, followed by a confirmation in
  your bot chat.

Safety rails: one application per posting, one message per contact per
cooldown, a daily cap, random gaps between sends, and an immediate pause of a
channel on flood/throttle errors (Telegram PEER_FLOOD pauses it for 24h). Every
record is committed to the database as it happens, so a crash can never cause a
repeat.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..ai import ApplicationDraft
from ..config import Config
from ..delivery.digest import _esc
from ..filters import match_keywords
from ..models import Posting
from ..state import State, iso, parse_iso, utcnow
from . import contacts as contact_rules
from . import language
from .senders import ChannelUnavailable, ContactError, EmailSender, Sender, TelegramUserSender

log = logging.getLogger("jobradar.apply")

MODES = ("off", "dry_run", "auto")
CHANNELS = ("telegram", "email")
_DONE = ("sent", "dry_run")      # statuses that count toward the daily cap
MAX_DRAFT_ATTEMPTS = 3
_CHANNEL_LABEL = {"telegram": "Telegram", "email": "Email"}

Drafter = Callable[[dict[str, Any]], "ApplicationDraft | None"]
Notifier = Callable[[str], None]


@dataclass
class ApplyReport:
    mode: str = "off"
    queued: int = 0
    sent: int = 0
    previewed: int = 0
    skipped_fit: int = 0
    skipped_cooldown: int = 0
    failed: int = 0
    expired: int = 0
    waiting: int = 0
    alerts: list[str] = field(default_factory=list)


def _fmt(template: str, **values: str) -> str:
    class _Keep(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"
    return template.format_map(_Keep(values))


def template_draft(config: Config, entry: dict[str, Any], name: str | None = None) -> ApplicationDraft:
    """Your fixed template (Chinese or English per message_language), used when
    AI is off or unavailable."""
    lang = language.resolve(config.message_language, entry.get("description") or "")
    title = entry.get("title") or ("该职位" if lang == "zh" else "this role")
    template = config.apply_template_zh if lang == "zh" else config.apply_template
    subject = config.apply_email_subject_zh if lang == "zh" else config.apply_email_subject
    body = _fmt(template, title=title, name=name or config.applicant_name or "")
    return ApplicationDraft(
        fit_score=None, fit_reason="template (no AI fit check)", message=body.strip(),
        email_subject=_fmt(subject, title=title)[:150], email_body=body.strip(),
        language=lang, source="template",
    )


class Applier:
    def __init__(
        self,
        config: Config,
        state: State,
        *,
        senders: dict[str, Sender],
        drafter: Drafter,
        notify: Notifier | None = None,
        resume_pdf: Path | None = None,
        resume_builder: Any = None,
        ai_available: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[float, float], float] = random.uniform,
    ):
        self.config = config
        self.state = state
        self.senders = senders
        self.drafter = drafter
        self.notify = notify
        self.resume_pdf = resume_pdf
        self.resume_builder = resume_builder   # makes a tailored PDF per application (resume.builder)
        self.ai_available = ai_available
        self.sleep = sleep
        self.rand = rand

    @property
    def mode(self) -> str:
        m = (self.config.apply_mode or "off").strip().lower()
        return m if m in MODES else "off"

    # ------------------------------------------------------------- public
    def run(self, postings: list[Posting], now: datetime | None = None) -> ApplyReport:
        now = now or utcnow()
        report = ApplyReport(mode=self.mode)
        if self.mode == "off":
            return report
        if self.mode == "auto" and self.resume_builder is None and not (self.resume_pdf and self.resume_pdf.exists()):
            self._alert(report, "Auto-apply is on but there's no résumé: fill in your profile (name, job title, "
                                f"education) or upload a PDF ({self.config.resume_pdf_path}). Nothing was sent.")
            report.queued = self.enqueue(postings, now)
            report.waiting = self.state.queue_len()
            return report
        try:
            report.queued = self.enqueue(postings, now)
            self.process(now, report)
        finally:
            for s in self.senders.values():
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    log.exception("closing %s sender failed", s.channel)
        report.waiting = self.state.queue_len()
        return report

    # ------------------------------------------------------------ enqueue
    def enqueue(self, postings: list[Posting], now: datetime) -> int:
        cfg = self.config
        if not self.ai_available and not cfg.apply_keywords:
            if postings:
                log.warning("auto-apply: AI is off, so set apply_keywords to say which roles "
                            "to apply to — nothing was queued")
            return 0
        cutoff = now - timedelta(days=cfg.apply_max_age_days) if cfg.apply_max_age_days > 0 else None
        added = 0
        for p in postings:
            key = p.content_hash
            if not key or self.state.has_application(key) or self.state.queue_has(key):
                continue
            if cutoff and p.posted_at and p.posted_at < cutoff:
                continue
            found = contact_rules.extract(p.description, source_type=p.source_type,
                                          source_url=p.source_url, extra=p.contact)
            usable = {ch: found.for_channel(ch) for ch in self._channels() if found.for_channel(ch)}
            if not usable:
                continue
            hay = f"{p.title}\n{p.description}"
            if cfg.apply_exclude_keywords and match_keywords(hay, cfg.apply_exclude_keywords):
                continue
            if cfg.apply_keywords and not match_keywords(hay, cfg.apply_keywords):
                continue
            self.state.queue_put(key, {
                "title": p.title, "description": p.description[:8000], "source": p.source,
                "source_url": p.source_url, "posted_at": iso(p.posted_at), "queued_at": iso(now),
                "attempts": 0, **usable,
            })
            added += 1
        return added

    # ------------------------------------------------------------ process
    def process(self, now: datetime, report: ApplyReport) -> None:
        cfg = self.config
        if self.mode == "dry_run":
            # A dry run contacts nobody, so it works before Telegram/SMTP are set up.
            active: dict[str, Any] = {ch: self.senders.get(ch) for ch in self._channels()}
        else:
            active = {ch: s for ch, s in self.senders.items()
                      if ch in self._channels() and not self._paused(ch, now)}
        remaining = max(0, cfg.apply_daily_cap - self._used_last_24h(now))

        cutoff = now - timedelta(days=cfg.apply_max_age_days) if cfg.apply_max_age_days > 0 else None
        order = []
        for key, entry in self.state.queue_items():  # newest posting first
            posted = parse_iso(entry.get("posted_at"))
            if cutoff and posted and posted < cutoff:
                self.state.queue_remove(key)
                report.expired += 1
            else:
                order.append((key, entry))

        for key, entry in order:
            if remaining <= 0 or not active:
                break
            chans = [ch for ch in self._channels() if entry.get(ch) and ch in active]
            if not chans:
                continue  # its channels are paused/unconfigured: wait
            fresh = [ch for ch in chans if not self._cooling_down(ch, entry[ch], now)]
            if not fresh:
                self._finish(key, entry, now, status="skipped_cooldown",
                             contact=entry[chans[0]], channel=chans[0])
                report.skipped_cooldown += 1
                continue

            draft = self.drafter(entry)
            if draft is None:
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                if entry["attempts"] >= MAX_DRAFT_ATTEMPTS:
                    self._finish(key, entry, now, status="failed", error="could not draft")
                    report.failed += 1
                else:
                    self.state.queue_put(key, entry)
                continue
            if draft.fit_score is not None and draft.fit_score < cfg.apply_min_score:
                self._finish(key, entry, now, status="skipped_fit", score=draft.fit_score,
                             reason=draft.fit_reason)
                report.skipped_fit += 1
                log.info("auto-apply: skipped %r (fit %s < %s): %s", entry.get("title"),
                         draft.fit_score, cfg.apply_min_score, draft.fit_reason)
                continue

            attachment, resume_file = self._resume_for(key, entry)
            if attachment is None and self.mode == "auto":   # never send an application without a résumé
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                if entry["attempts"] >= MAX_DRAFT_ATTEMPTS:
                    self._finish(key, entry, now, status="failed", error="could not make the résumé")
                    report.failed += 1
                else:
                    self.state.queue_put(key, entry)
                continue

            outcome = self._deliver(key, entry, draft, fresh, active, now, report, attachment, resume_file)
            if outcome == "done":
                remaining -= 1
                # (Each record above is already committed to the database, so a
                # crash here can never cause the same application to be re-sent.)
                if self.mode == "auto" and remaining > 0:
                    lo, hi = (list(cfg.apply_delay_seconds) + [0.0, 0.0])[:2]
                    self.sleep(max(0.0, self.rand(float(lo), float(max(lo, hi)))))

    # ------------------------------------------------------------ helpers
    def _resume_for(self, key: str, entry: dict[str, Any]) -> tuple[Path | None, str]:
        """The résumé to attach: a fresh one tailored to this posting from your
        profile, else your uploaded PDF. (path, path relative to the data folder)."""
        if self.resume_builder is not None:
            try:
                path = self.resume_builder.build(
                    f"{entry.get('title') or ''}\n{entry.get('description') or ''}", key=key)
                return path, self.resume_builder.relative(path)
            except Exception as e:  # noqa: BLE001 - fall back to the uploaded PDF, if any
                log.warning("auto-apply: couldn't make the résumé: %s", e)
                self._notify(language.t("resume_failed", self._lang(), error=_esc(str(e))[:300]))
        if self.resume_pdf and self.resume_pdf.exists():
            return self.resume_pdf, ""
        return None, ""

    def _deliver(self, key: str, entry: dict[str, Any], draft: ApplicationDraft,
                 channels: list[str], active: dict[str, Sender], now: datetime,
                 report: ApplyReport, attachment: Path | None = None, resume_file: str = "") -> str:
        errors: list[str] = []
        blocked = False
        for ch in channels:
            if ch not in active:
                blocked = True
                continue
            contact = entry[ch]
            text = draft.message if ch == "telegram" else draft.email_body
            subject = draft.email_subject or _fmt(self.config.apply_email_subject,
                                                  title=entry.get("title") or "")
            if self.mode == "dry_run":
                self._notify(self._preview(entry, draft, ch, contact, subject, text, attachment))
                self._finish(key, entry, now, status="dry_run", channel=ch, contact=contact,
                             score=draft.fit_score, reason=draft.fit_reason, message=text,
                             resume_file=resume_file)
                self._mark_contact(ch, contact, now)
                report.previewed += 1
                return "done"
            try:
                sent = active[ch].send(contact, message=text, subject=subject, attachment=attachment)
            except ContactError as e:
                errors.append(str(e))
                log.warning("auto-apply: %s", e)
                continue
            except ChannelUnavailable as e:
                blocked = True
                active.pop(ch, None)
                if e.pause_seconds:
                    self.state.set_paused(ch, iso(now + timedelta(seconds=e.pause_seconds)))
                until = f" until {self.state.paused_until(ch)[:16]}Z" if e.pause_seconds else " for this run"
                self._alert(report, f"Auto-apply: {_CHANNEL_LABEL[ch]} paused{until} — {e}")
                continue
            self._finish(key, entry, now, status="sent", channel=ch, contact=contact,
                         score=draft.fit_score, reason=draft.fit_reason, message=text, resume_file=resume_file)
            self._record_conversation(key, entry, ch, contact, text, subject, sent, now, attachment)
            self._mark_contact(ch, contact, now)
            report.sent += 1
            self._notify(self._confirmation(entry, draft, ch, contact, text))
            return "done"
        if blocked:
            return "waiting"  # a channel may come back; keep it queued
        self._finish(key, entry, now, status="failed", error="; ".join(errors)[:300])
        report.failed += 1
        return "failed"

    def _channels(self) -> list[str]:
        return [c for c in (ch.strip().lower() for ch in self.config.apply_channels) if c in CHANNELS]

    def _paused(self, channel: str, now: datetime) -> bool:
        until = parse_iso(self.state.paused_until(channel))
        if until and until > now:
            log.info("auto-apply: %s paused until %s", channel, iso(until))
            return True
        if until:
            self.state.clear_paused(channel)
        return False

    def _used_last_24h(self, now: datetime) -> int:
        return self.state.count_applications(mode=self.mode, statuses=_DONE,
                                             since=now - timedelta(hours=24))

    @staticmethod
    def _contact_key(channel: str, contact: str) -> str:
        return f"{'tg' if channel == 'telegram' else channel}:{contact.lower()}"

    def _cooling_down(self, channel: str, contact: str, now: datetime) -> bool:
        days = self.config.apply_contact_cooldown_days
        last = parse_iso(self.state.contact_last(self._contact_key(channel, contact), self.mode))
        return bool(days > 0 and last and last > now - timedelta(days=days))

    def _mark_contact(self, channel: str, contact: str, now: datetime) -> None:
        self.state.mark_contact(self._contact_key(channel, contact), self.mode, now)

    def _finish(self, key: str, entry: dict[str, Any], now: datetime, *, status: str,
                channel: str | None = None, contact: str | None = None,
                score: int | None = None, reason: str = "", error: str = "", message: str = "",
                resume_file: str = "") -> None:
        self.state.finish_application(key, {
            "at": iso(now), "mode": self.mode, "status": status, "channel": channel,
            "contact": contact, "title": (entry.get("title") or "")[:200],
            "source_url": entry.get("source_url"), "score": score, "reason": reason[:300],
            "error": error, "message": message, "resume_file": resume_file,
        })

    def _record_conversation(self, key: str, entry: dict[str, Any], channel: str, contact: str,
                             text: str, subject: str, sent, now: datetime, attachment: Path | None = None) -> None:
        """Keep the sent application in that representative's conversation, so
        the dashboard shows it (and their replies, once synced)."""
        try:
            conv = self.state.conversation_id(channel, contact, display_name=getattr(sent, "display_name", ""),
                                              peer_id=getattr(sent, "peer_id", None))
            title = (entry.get("title") or "")[:200]
            resume = attachment.name if attachment else ""
            if channel == "email":
                self.state.add_message(conv, "out", text, sent_at=now, subject=subject, attachment=resume,
                                       external_id=getattr(sent, "text_id", None), job_fingerprint=key,
                                       job_title=title)
            else:
                self.state.add_message(conv, "out", text, sent_at=now, external_id=getattr(sent, "text_id", None),
                                       job_fingerprint=key, job_title=title)
                if resume:
                    self.state.add_message(conv, "out", "", sent_at=now, attachment=resume,
                                           external_id=getattr(sent, "file_id", None), job_fingerprint=key,
                                           job_title=title)
        except Exception:  # noqa: BLE001 - the application itself went out fine
            log.exception("couldn't record the conversation")

    def _alert(self, report: ApplyReport, message: str) -> None:
        log.warning(message)
        report.alerts.append(message)
        self._notify(f"⚠️ {_esc(message)}")

    def _notify(self, text: str) -> None:
        if not self.notify:
            return
        try:
            self.notify(text)
        except Exception:  # noqa: BLE001 - a failed notice must not stop applying
            log.exception("auto-apply notification failed")

    def _lang(self) -> str:
        return language.ui(self.config.message_language)

    def _fit(self, draft: ApplicationDraft) -> str:
        lang = self._lang()
        return (language.t("fit", lang, score=str(draft.fit_score)) if draft.fit_score is not None
                else language.t("template", lang))

    def _channel(self, channel: str) -> str:
        return language.t("channel_" + channel, self._lang())

    def _preview(self, entry: dict[str, Any], draft: ApplicationDraft, channel: str,
                 contact: str, subject: str, text: str, attachment: Path | None = None) -> str:
        lang = self._lang()
        if attachment is not None and self.resume_builder is not None and attachment != self.resume_pdf:
            resume = language.t("resume_tailored", lang, file=_esc(attachment.name))
        else:
            resume = f"📎 {_esc(attachment.name)}" if attachment else language.t("no_resume", lang)
        lines = [
            language.t("dry_run", lang, fit=self._fit(draft)),
            f"<b>{_esc(entry.get('title') or '(untitled)')}</b>",
            language.t("to", lang, channel=self._channel(channel), contact=_esc(contact)),
        ]
        if draft.fit_reason:
            lines.append(language.t("why", lang, reason=_esc(draft.fit_reason)))
        if entry.get("source_url"):
            lines.append(f"🔗 {_esc(entry['source_url'])}")
        lines.append("")
        if channel == "email":
            lines.append(language.t("subject", lang, subject=_esc(subject)))
        lines.append(_esc(text[:2500]))
        lines.append("\n" + resume)
        return "\n".join(lines)

    def _confirmation(self, entry: dict[str, Any], draft: ApplicationDraft, channel: str,
                      contact: str, text: str) -> str:
        lines = [
            language.t("applied", self._lang(), channel=self._channel(channel), contact=_esc(contact),
                       fit=self._fit(draft)),
            f"<b>{_esc(entry.get('title') or '(untitled)')}</b>",
        ]
        if entry.get("source_url"):
            lines.append(f"🔗 {_esc(entry['source_url'])}")
        lines += ["", _esc(text[:1500])]
        return "\n".join(lines)


# --------------------------------------------------------------- building
def _resolve(base: Path, p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p).expanduser()
    return path if path.is_absolute() else base / path


def ai_key(config: Config) -> str | None:
    if (config.ai_provider or "claude").lower() == "gemini":
        return config.gemini_api_key
    return config.anthropic_api_key


def build_senders(config: Config, base: Path) -> dict[str, Sender]:
    senders: dict[str, Sender] = {}
    if config.telegram_api_id and config.telegram_api_hash:
        senders["telegram"] = TelegramUserSender(
            api_id=config.telegram_api_id, api_hash=config.telegram_api_hash,
            session_path=str(_resolve(base, config.telegram_session_path)))
    if config.smtp_host and (config.smtp_from or config.smtp_user):
        senders["email"] = EmailSender(
            host=config.smtp_host, port=int(config.smtp_port), user=config.smtp_user,
            password=config.smtp_password, sender=config.smtp_from or config.smtp_user or "",
            use_ssl=bool(config.smtp_ssl))
    return senders


def build_applier(config: Config, state: State, *, base_dir: Path,
                  notify: Notifier | None = None) -> Applier:
    from .. import ai

    from ..resume import builder as resume_builder
    from ..resume import profile as prof

    resume_pdf = _resolve(base_dir, config.resume_pdf_path)
    text_path = _resolve(base_dir, config.resume_text_path)
    resume_text = text_path.read_text("utf-8") if text_path and text_path.exists() else None
    builder = None
    if (config.resume_source or "generated") == "generated":
        profile = state.get_profile()
        builder = resume_builder.make_builder(config, base_dir, profile)
        if builder is not None:
            resume_text = prof.facts_text(builder.profile)   # the AI judges fit from your profile
        elif resume_pdf and resume_pdf.exists():
            log.info("auto-apply: your profile is incomplete, so your uploaded résumé is used")
    key = ai_key(config)
    ai_on = bool(config.use_ai and key and (resume_text or (resume_pdf and resume_pdf.exists())))
    pdf_bytes: bytes | None = None

    def drafter(entry: dict[str, Any]) -> ApplicationDraft | None:
        nonlocal pdf_bytes
        if ai_on:
            if resume_text is None and pdf_bytes is None and resume_pdf:
                pdf_bytes = resume_pdf.read_bytes()
            d = ai.draft_application(
                entry.get("description") or "", resume_text=resume_text, resume_pdf=pdf_bytes,
                applicant_name=(builder.profile["name"] if builder else config.applicant_name),
                provider=config.ai_provider,
                api_key=key or "", model=config.ai_model, max_chars=config.ai_max_chars,
                language=config.message_language)
            if d is not None:
                return d
            # AI failed: the template is only a safe stand-in when a keyword gate
            # already narrowed the roles; otherwise retry next run.
            if not config.apply_keywords:
                return None
        return template_draft(config, entry, name=builder.profile["name"] if builder else None)

    return Applier(config, state, senders=build_senders(config, base_dir), drafter=drafter,
                   notify=notify, resume_pdf=resume_pdf, resume_builder=builder, ai_available=ai_on)
