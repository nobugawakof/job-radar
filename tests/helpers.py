"""Test helpers: an in-memory collector and a Service factory.

The in-memory collector lets the acceptance tests drive the full pipeline
(collect → classify → extract → dedup → filter → deliver) without touching the
network, which is exactly the isolation NFR-13/NFR-15 are meant to provide.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from jobradar.collectors.base import Collector, CollectorError, FetchContext, HttpClient
from jobradar.collectors.registry import register
from jobradar.config import Config
from jobradar.models import RawItem
from jobradar.service import Service


# source name -> list of RawItem (or a callable raising to simulate failure)
MEMORY_ITEMS: dict[str, Any] = {}


@register("memory")
class MemoryCollector(Collector):
    type = "memory"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        payload = MEMORY_ITEMS.get(self.name, [])
        if callable(payload):
            payload = payload()  # may raise CollectorError to simulate a break
        for item in payload:
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item


def make_service(tmpdir: str | None = None) -> Service:
    tmpdir = tmpdir or tempfile.mkdtemp()
    db_path = str(Path(tmpdir) / "test.db")
    cfg = Config(db_path=db_path, backup_dir=str(Path(tmpdir) / "backups"))
    return Service(cfg)


def raw(source: str, ext_id: str, text: str, *, url: str = "https://example.com/x",
        posted_at: datetime | None = None, title: str | None = None) -> RawItem:
    return RawItem(
        source=source, external_id=ext_id, raw_text=text, url=url,
        posted_at=posted_at or datetime.now(timezone.utc), title_hint=title,
    )


class FakeTransport:
    """Records outgoing Telegram messages; can be told to fail N times (NFR-5)."""

    def __init__(self, fail_times: int = 0):
        self.sent: list[dict[str, Any]] = []
        self.callbacks: list[tuple[str, str]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail_times = fail_times

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated telegram outage")
        rec = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.sent.append(rec)
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))

    def get_updates(self, offset=None, timeout=0):
        out = self.updates
        self.updates = []
        return out
