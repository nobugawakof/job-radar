"""Bluesky collector (Tier A).

Uses the public AT Protocol HTTP API (``app.bsky.feed.searchPosts``) — no key
required. We search for hiring-related terms and hand the raw post text to the
pipeline. The application URL is reconstructed from the post's AT-URI.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import quote

from ..state import parse_iso
from ..models import RawItem
from .base import Collector, FetchContext, HttpClient
from .registry import register

_SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


@register("bluesky")
class BlueskyCollector(Collector):
    type = "bluesky"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        self.query = config.get("query", "hiring remote")

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        url = f"{_SEARCH_URL}?q={quote(self.query)}&limit=100&sort=latest"
        status, body = self.http.get(url)
        for item in self.parse(body, self.name):
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item

    @staticmethod
    def parse(body: bytes, source_name: str) -> Iterator[RawItem]:
        data = json.loads(body)
        for post in data.get("posts", []):
            record = post.get("record", {})
            text = (record.get("text") or "").strip()
            if not text:
                continue
            uri = post.get("uri", "")
            handle = (post.get("author", {}) or {}).get("handle", "")
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            web_url = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else uri
            posted = parse_iso(record.get("createdAt") or post.get("indexedAt"))
            yield RawItem(
                source=source_name,
                external_id=uri or f"{handle}/{rkey}",
                raw_text=text,
                url=web_url,
                raw_json=json.dumps(post),
                posted_at=posted,
            )
