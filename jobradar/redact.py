"""Strip secrets from text before it reaches logs or the dashboard (NFR-10)."""

from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"(api\.telegram\.org/bot)[0-9]+:[A-Za-z0-9_-]+"), r"\1<token>"),
    (re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"), "<telegram-bot-token>"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), "<anthropic-key>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "<google-key>"),
    (re.compile(r"([?&](?:key|token|access_token|api_key)=)[^&\s]+", re.I), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


def mask_contact(contact: str | None) -> str:
    """Hide most of a hiring contact for logs (SRS DR-4): "@hr_anna" →
    "@h***na", "jane.doe@acme.io" → "j***@acme.io". The full value stays in
    the database, which needs it, and is shown only in your own dashboard."""
    c = (contact or "").strip()
    if not c:
        return ""
    if "@" in c and not c.startswith("@"):
        local, _, domain = c.partition("@")
        return f"{local[:1]}***@{domain}"
    handle = c.lstrip("@")
    shown = handle[:1] + "***" + (handle[-2:] if len(handle) > 4 else "")
    return ("@" if c.startswith("@") else "") + shown
