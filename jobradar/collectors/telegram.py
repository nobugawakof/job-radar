"""Telegram channel collector (Tier A).

Telegram's Bot API delivers channel posts to a bot that has been added to the
channel. This collector consumes ``channel_post`` updates via ``getUpdates``,
filters them to the configured channel, and yields each as a raw item. It
shares the bot token used for delivery (D-2), read from the environment (DR-6).

Note: the Bot API does not expose historical channel messages, so this
collector sees posts from the point the bot joined onward — acceptable for a
continuously-running collector, and the catch-up logic (FR-4) covers gaps
where the bot itself was reachable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

from ..models import RawItem
from .base import Collector, CollectorError, FetchContext, HttpClient
from .registry import register


@register("telegram")
class TelegramChannelCollector(Collector):
    type = "telegram"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        # Channel username (without @) or numeric id this source represents.
        self.channel = str(config.get("channel", "")).lstrip("@")

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        token = os.environ.get("JOBRADAR_TELEGRAM_BOT_TOKEN")
        if not token:
            raise CollectorError("Telegram bot token missing (JOBRADAR_TELEGRAM_BOT_TOKEN, DR-6)")
        url = f"https://api.telegram.org/bot{token}/getUpdates?allowed_updates=[\"channel_post\"]&limit=100"
        status, body = self.http.get(url)
        for item in self.parse(body, self.name, self.channel):
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item

    @staticmethod
    def parse(body: bytes, source_name: str, channel: str) -> Iterator[RawItem]:
        data = json.loads(body)
        if not data.get("ok", False):
            raise CollectorError(f"Telegram getUpdates error: {data.get('description')}")
        for upd in data.get("result", []):
            post = upd.get("channel_post") or upd.get("message")
            if not post:
                continue
            chat = post.get("chat", {})
            username = str(chat.get("username", "")).lstrip("@")
            if channel and username and channel.lower() != username.lower():
                continue
            text = (post.get("text") or post.get("caption") or "").strip()
            if not text:
                continue
            msg_id = post.get("message_id")
            ts = post.get("date")
            posted = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
            link = f"https://t.me/{username}/{msg_id}" if username and msg_id else None
            yield RawItem(
                source=source_name,
                external_id=f"{chat.get('id')}/{msg_id}",
                raw_text=text,
                url=link,
                raw_json=json.dumps(post),
                posted_at=posted,
            )
