"""Telegram channel collector (Tier A).

Reads a public Telegram channel's posts, e.g. ``t.me/remote_jobs`` in your
site list. Two ways, tried in order:

1. **Your own Telegram account** (the login auto-apply uses; Telethon). Reads
   any *public* channel's recent history without joining it — the useful
   case, since job channels belong to other people.
2. **Your bot**, for channels it has been added to (as an admin). The bot
   inbox (:mod:`jobradar.delivery.inbox`) files those posts as they arrive;
   this collector reads them back.

If neither is available the source fails with a message saying what to set up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..models import RawItem
from .base import Collector, CollectorError, FetchContext, HttpClient
from .registry import register

log = logging.getLogger("jobradar.collectors.telegram")
MAX_MESSAGES = 200


def _account_messages(cfg: dict[str, Any], channel: str, since: datetime | None,
                      limit: int = MAX_MESSAGES) -> list[tuple[int, datetime, str]] | None:
    """Recent posts of a public channel through your own account, newest
    first, stopping at ``since``. None if the account isn't set up/logged in."""
    api_id, api_hash = cfg.get("telegram_api_id"), cfg.get("telegram_api_hash")
    session = str(cfg.get("telegram_session_path") or "")
    if not (api_id and api_hash and session):
        return None
    if not Path(session if session.endswith(".session") else session + ".session").exists():
        return None
    from ..tgclient import LoopThread, TelethonMissing, make_client

    runner = LoopThread("tg-channel")
    client = None
    try:
        try:
            client = make_client(runner, session, api_id, api_hash)
        except TelethonMissing:
            return None
        runner.call(lambda: client.connect(), 30)
        if not runner.call(lambda: client.is_user_authorized(), 15):
            return None

        async def read():
            out = []
            async for m in client.iter_messages(channel, limit=limit):
                if since and m.date and m.date <= since:
                    break
                text = (getattr(m, "message", None) or "").strip()
                if text:
                    out.append((int(m.id), m.date, text))
            return out

        return runner.call(read, 90)
    except TimeoutError as e:
        raise CollectorError(f"Telegram didn't answer in time reading @{channel}") from e
    except CollectorError:
        raise
    except Exception as e:  # noqa: BLE001 - Telethon errors: private/unknown channel, flood wait…
        name = type(e).__name__
        if name in ("UsernameInvalidError", "UsernameNotOccupiedError", "ValueError", "ChannelPrivateError"):
            raise CollectorError(f"@{channel} isn't a public channel (check the name, or it may be private)") from e
        if name == "FloodWaitError":
            raise CollectorError(f"Telegram asks to wait {getattr(e, 'seconds', '?')}s before reading more") from e
        raise CollectorError(f"couldn't read @{channel}: {name}: {e}") from e
    finally:
        if client is not None:
            try:
                runner.call(lambda: client.disconnect(), 10)
            except Exception:  # noqa: BLE001
                pass
        runner.stop()


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

    def _item(self, msg_id: int, date: datetime, text: str) -> RawItem:
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        url = f"https://t.me/{self.channel}/{msg_id}" if not self.channel.lstrip("-").isdigit() else None
        return RawItem(source=self.name, external_id=f"{self.channel}/{msg_id}", raw_text=text,
                       url=url, posted_at=date)

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        if not self.channel:
            raise CollectorError("no channel set for this Telegram source")
        msgs = _account_messages(self.config, self.channel, ctx.since)
        if msgs is not None:
            for msg_id, date, text in msgs:
                yield self._item(msg_id, date, text)
            return
        if ctx.bot_posts is None:
            raise CollectorError(
                f"can't read @{self.channel}: log in your Telegram account in the dashboard "
                "(Auto-apply → Your Telegram account) to read public channels, or add your bot to the channel")
        for p in ctx.bot_posts(self.channel, ctx.since):
            yield self._item(p["message_id"], datetime.fromisoformat(p["posted_at"]), p["text"])
