"""Message language (Chinese by default) and the few phrases Job Radar itself
writes in Telegram (auto-apply and reply notices) and in the generated résumé.

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
    "resume_tailored": {"zh": "📎 已为该职位定制简历：{file}", "en": "📎 Résumé tailored for this job: {file}"},
    "resume_failed": {"zh": "⚠️ 简历生成失败：{error}", "en": "⚠️ Couldn't make the résumé: {error}"},
    # The résumé itself (sent to recruiters)
    "cv_intro": {"zh": "职业概述", "en": "Professional Summary"},
    "cv_skills": {"zh": "技术技能", "en": "Technical Skills"},
    "cv_experience": {"zh": "工作经历", "en": "Work Experience"},
    "cv_projects": {"zh": "重点项目", "en": "Key Projects"},
    "cv_stack": {"zh": "技术栈：", "en": "Tech stack: "},
    "cv_role": {"zh": "角色：", "en": "Role: "},
    "cv_education": {"zh": "教育背景", "en": "Education"},
    "cv_languages": {"zh": "语言能力", "en": "Languages"},
    "cv_present": {"zh": "至今", "en": "Present"},
    "cv_male": {"zh": "男", "en": "Male"},
    "cv_female": {"zh": "女", "en": "Female"},
    "cv_born": {"zh": "出生：{date}", "en": "Born {date}"},
    "cv_file": {"zh": "{name}_简历", "en": "{name}_Resume"},
}


def t(key: str, lang: str, **values: str) -> str:
    return PHRASES[key][lang if lang in ("zh", "en") else "zh"].format(**values)
