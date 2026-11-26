"""Message language (Chinese by default) and the few phrases Job Radar itself
writes in Telegram: auto-apply notices and reply notices.

``message_language`` in the config:

* ``"zh"`` (default) — applications are written in Simplified Chinese;
* ``"en"`` — in English;
* ``"auto"`` — in the job post's own language.
"""

from __future__ import annotations

import re

LANGUAGES = ("zh", "en", "auto")
_CJK = re.compile(r"[一-鿿]")


def resolve(setting: str | None, text: str = "") -> str:
    """"zh" or "en" for one message, given the setting and the job post."""
    s = (setting or "zh").strip().lower()
    if s == "en":
        return "en"
    if s == "auto":
        cjk = len(_CJK.findall(text or ""))
        return "zh" if cjk >= 10 or (text and cjk / max(len(text), 1) > 0.05) else "en"
    return "zh"


def ui(setting: str | None) -> str:
    """Language for Job Radar's own notices: English only if chosen, else Chinese."""
    return "en" if (setting or "zh").strip().lower() == "en" else "zh"


PHRASES = {
    "dry_run": {"zh": "📝 <b>演练</b> — 将会投递（{fit}）", "en": "📝 <b>DRY RUN</b> — would apply ({fit})"},
    "applied": {"zh": "✅ <b>已投递</b>，通过 {channel} {contact}（{fit}）",
                "en": "✅ <b>Applied</b> via {channel} {contact} ({fit})"},
    "to": {"zh": "发送给：{channel} {contact}", "en": "To: {channel} {contact}"},
    "why": {"zh": "理由：{reason}", "en": "Why: {reason}"},
    "subject": {"zh": "主题：{subject}", "en": "Subject: {subject}"},
    "fit": {"zh": "匹配度 {score}/100", "en": "fit {score}/100"},
    "template": {"zh": "模板", "en": "template"},
    "no_resume": {"zh": "⚠️ 未找到简历 PDF", "en": "⚠️ résumé PDF not found"},
    "reply": {"zh": "💬 <b>{who}</b> 回复了你{job}：\n{text}", "en": "💬 <b>{who}</b> replied{job}:\n{text}"},
    "reply_job": {"zh": "（{title}）", "en": " ({title})"},
    "channel_telegram": {"zh": "Telegram", "en": "Telegram"},
    "channel_email": {"zh": "邮件", "en": "Email"},
}


def t(key: str, lang: str, **values: str) -> str:
    return PHRASES[key][lang if lang in ("zh", "en") else "zh"].format(**values)
