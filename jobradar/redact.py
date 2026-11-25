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
