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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

log = logging.getLogger("jobradar.telegram")


class TelegramRateLimited(RuntimeError):
    """Telegram returned 429 and we exhausted our in-run retries. Not a
    formatting problem, so the caller should not retry as plain text — the
    pipeline holds the remaining postings for the next run."""


def _to_plain(html_text: str) -> str:
    """Turn our HTML-formatted message into readable plain text for the fallback
    send (drops <b> tags and un-escapes entities so they don't show literally)."""
    t = re.sub(r"</?b>", "", html_text)
    return (t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


class Transport(Protocol):
    def send_message(self, chat_id: str, text: str) -> dict[str, Any]: ...


class TelegramTransport:
    """Real Telegram Bot API sender (stdlib urllib, HTTPS only)."""

    # Telegram's hard limit is 4096 UTF-16 units; stay safely under it.
    MAX_TEXT = 4000

    def __init__(self, token: str, timeout: float = 20.0):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    # How many times to wait-and-retry on a Telegram 429 before giving up and
    # letting the pipeline hold the rest for the next run.
    MAX_429_RETRIES = 3

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.MAX_429_RETRIES + 1):
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(f"{self.base}/sendMessage", data=data, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # Telegram puts the real reason in the body ("message is too
                # long", "can't parse entities…", "chat not found", …), and for
                # 429 a parameters.retry_after telling us how long to wait.
                body = e.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(body)
                    desc = parsed.get("description", body)
                except (json.JSONDecodeError, AttributeError):
                    parsed, desc = {}, body
                if e.code == 429 and attempt < self.MAX_429_RETRIES:
                    retry_after = int((parsed.get("parameters") or {}).get("retry_after") or 5)
                    log.warning("Telegram flood limit; waiting %ss then retrying "
                                "(%d/%d)", retry_after, attempt + 1, self.MAX_429_RETRIES)
                    time.sleep(min(retry_after, 60))
                    continue
                if e.code == 429:
                    raise TelegramRateLimited(f"Telegram sendMessage 429: {desc}") from e
                raise RuntimeError(f"Telegram sendMessage {e.code}: {desc}") from e
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {payload.get('description')}")
            return payload.get("result", {})
        raise TelegramRateLimited("Telegram sendMessage 429: retries exhausted")

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        text = text[: self.MAX_TEXT]
        try:
            return self._call({
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
        except TelegramRateLimited:
            # A rate limit is not a formatting problem — don't waste a second
            # send as plain text; let the pipeline hold the rest for next run.
            raise
        except RuntimeError:
            # If HTML parsing (or anything else) rejected the message, retry once
            # as plain text so a single odd post can't block the whole digest.
            plain = _to_plain(text)[: self.MAX_TEXT]
            return self._call({
                "chat_id": chat_id, "text": plain, "disable_web_page_preview": "true",
            })


def send_all(transport: Transport, chat_id: str, messages: list[str]) -> None:
    """Send every message in order. Raises on the first failure so the caller
    can leave those postings unmarked and retry them next run."""
    for msg in messages:
        transport.send_message(chat_id, msg)
