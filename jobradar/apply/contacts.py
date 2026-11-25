"""Find the application contact a posting explicitly asks applicants to use.

This decides *who gets messaged*, so it is deliberately strict:

* A Telegram handle is taken only when the post clearly says it's a Telegram
  contact (``t.me/<user>``, ``Telegram: @user``, ``TG @user``, ``电报/飞机 @user``)
  — or, for posts that come *from* Telegram, when the handle is introduced as the
  contact (``Contact: @user``, ``DM @user``, ``联系 @user``). A bare ``@name`` on
  X/Bluesky/Reddit is that platform's handle, not a Telegram one, and is ignored.
* Bots (``…bot``), the source channel itself, and message/invite links are
  never treated as a person to contact.
* Emails exclude no-reply style addresses. HN-style ``name [at] company [dot]
  com`` obfuscation is decoded; prose like "at the company" is not.

The recipient always comes from these rules — never from the AI — so a posting
can't talk the drafter into messaging someone else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Telegram usernames: 5–32 chars, start with a letter, letters/digits/underscore.
_USER = r"([A-Za-z][A-Za-z0-9_]{4,31})"

_TME_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/" + _USER + r"(/\d+)?(?![A-Za-z0-9_/])", re.I)
# Labels that always mean Telegram, on any source.
_TG_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(?:telegram|tg|电报|飞机|纸飞机)\s*(?:id|账号|号)?\s*[:：]?\s*@" + _USER, re.I)
# Generic "contact" labels — trusted only for posts that came from Telegram.
_CONTACT_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(?:contact|contacts|dm|pm|message|msg|write to|reach out to|apply|send (?:your )?(?:cv|resume) to|"
    r"hr|recruiter|联系|联系人|联系方式|私信|私聊|投递|简历发|咨询)\s*(?:me\s+)?(?:at\s+|via\s+|to\s+)?[:：]?\s*@" + _USER,
    re.I)
_NOT_PEOPLE = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "setlanguage", "boost"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[(\{]\s*at\s*[\])\}]\s*([A-Za-z0-9\-]+(?:\s*[\[(\{]\s*dot\s*[\])\}]\s*[A-Za-z0-9\-]+)+)",
    re.I)
_DOT_RE = re.compile(r"\s*[\[(\{]\s*dot\s*[\])\}]\s*", re.I)
_NOREPLY_RE = re.compile(r"^(?:no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster)@", re.I)
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "domain.com", "email.com", "company.com"}
_APPLY_EMAIL_HINT = re.compile(r"^(?:jobs?|careers?|hiring|hr|recruit\w*|talent|apply|cv|resume|people)@", re.I)


@dataclass(frozen=True)
class ApplyContacts:
    telegram: str | None = None   # "@username"
    email: str | None = None

    def for_channel(self, channel: str) -> str | None:
        return {"telegram": self.telegram, "email": self.email}.get(channel)

    def any(self) -> bool:
        return bool(self.telegram or self.email)


def _source_channel(source_url: str | None) -> str | None:
    m = _TME_RE.search(source_url or "")
    return m.group(1).lower() if m else None


def _ok_handle(handle: str, own_channel: str | None) -> bool:
    h = handle.lower()
    return not (h.endswith("bot") or h in _NOT_PEOPLE or h == own_channel)


def extract_telegram(text: str, *, from_telegram: bool = False,
                     source_url: str | None = None) -> str | None:
    """Return "@username" the post explicitly offers as its Telegram contact."""
    own = _source_channel(source_url)
    for m in _TME_RE.finditer(text):
        if m.group(2):  # t.me/<channel>/<id> is a link to a post, not a person
            continue
        if _ok_handle(m.group(1), own):
            return "@" + m.group(1)
    patterns = [_TG_LABEL_RE] + ([_CONTACT_LABEL_RE] if from_telegram else [])
    for rx in patterns:
        for m in rx.finditer(text):
            if _ok_handle(m.group(1), own):
                return "@" + m.group(1)
    return None


def extract_email(text: str) -> str | None:
    """Return the best application email in the post, or None."""
    found: list[str] = []
    for m in _EMAIL_RE.finditer(text):
        found.append(m.group(0).rstrip("."))
    for m in _OBFUSCATED_RE.finditer(text):
        found.append(f"{m.group(1)}@{_DOT_RE.sub('.', m.group(2))}")
    good = [e for e in found
            if not _NOREPLY_RE.match(e) and e.split("@", 1)[1].lower() not in _PLACEHOLDER_DOMAINS]
    if not good:
        return None
    # Prefer an obvious hiring mailbox (jobs@, careers@, hr@ …) over e.g. press@.
    good.sort(key=lambda e: 0 if _APPLY_EMAIL_HINT.match(e) else 1)
    return good[0]


def extract(text: str, *, source_type: str = "", source_url: str | None = None,
            extra: str | None = None) -> ApplyContacts:
    """Find the Telegram handle and email a posting asks applicants to use.

    ``extra`` is additional text to search (e.g. the contact field the
    rule-based extractor or the AI already pulled out).
    """
    hay = "\n".join(t for t in (text, extra) if t)
    return ApplyContacts(
        telegram=extract_telegram(hay, from_telegram=(source_type == "telegram"),
                                  source_url=source_url),
        email=extract_email(hay),
    )
