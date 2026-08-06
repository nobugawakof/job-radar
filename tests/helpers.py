"""Test helpers: an in-memory collector, a Pipeline factory, a fake transport."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from jobradar.collectors.base import Collector, FetchContext, HttpClient
from jobradar.collectors.registry import register
from jobradar.config import Config
from jobradar.models import RawItem
from jobradar.pipeline import Pipeline
from jobradar.state import State


# source name -> list of RawItem, or a callable (may raise to simulate a break)
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
            payload = payload()
        for item in payload:
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item


class FakeTransport:
    """Records outgoing messages; can be told to fail N sends (outage)."""

    def __init__(self, fail_times: int = 0):
        self.sent: list[dict[str, str]] = []
        self.fail_times = fail_times

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated telegram outage")
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": len(self.sent)}


def make_pipeline(
    sources: list[dict[str, Any]],
    *,
    keywords: list[str] | None = None,
    remote_only: bool = True,
    transport: Any | None = None,
    tmpdir: str | None = None,
    chat_id: str | None = "42",
) -> tuple[Pipeline, State, Config]:
    tmpdir = tmpdir or tempfile.mkdtemp()
    state = State(str(Path(tmpdir) / "state.json"))
    cfg = Config(
        keywords=keywords if keywords is not None else ["backend"],
        remote_only=remote_only,
        telegram_chat_id=chat_id,
        state_path=str(Path(tmpdir) / "state.json"),
    )
    pipe = Pipeline(cfg, state, sources, transport=transport)
    return pipe, state, cfg


def raw(source: str, ext_id: str, text: str, *, url: str = "https://example.com/x",
        posted_at: datetime | None = None, title: str | None = None, dated: bool = True) -> RawItem:
    # dated=False yields an undated item (like a scraped page with no timestamp),
    # which every run re-emits — exercising the fingerprint dedup, not the
    # source watermark.
    when = posted_at or (datetime.now(timezone.utc) if dated else None)
    return RawItem(
        source=source, external_id=ext_id, raw_text=text, url=url,
        posted_at=when, title_hint=title,
    )


def mem_source(name: str, tier: str = "A", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "memory", "tier": tier, **extra}
