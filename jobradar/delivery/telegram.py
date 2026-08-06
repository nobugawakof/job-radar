"""Telegram — send only.

The Telegram-only build pushes digests to a single chat; it does not read
inbound messages, run a command menu, or keep a review queue. The transport is
abstracted behind a tiny interface so tests can drive it with a fake and so a
Telegram outage is contained (a failed send simply isn't marked as sent, and
the postings are retried next run).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Protocol

log = logging.getLogger("jobradar.telegram")


class Transport(Protocol):
    def send_message(self, chat_id: str, text: str) -> dict[str, Any]: ...


class TelegramTransport:
    """Real Telegram Bot API sender (stdlib urllib, HTTPS only)."""

    def __init__(self, token: str, timeout: float = 20.0):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.base}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {payload.get('description')}")
        return payload.get("result", {})


def send_all(transport: Transport, chat_id: str, messages: list[str]) -> None:
    """Send every message in order. Raises on the first failure so the caller
    can leave those postings unmarked and retry them next run."""
    for msg in messages:
        transport.send_message(chat_id, msg)
