"""Hacker News collector (Tier A).

Uses the free Algolia HN Search API to pull comments from the monthly
"Ask HN: Who Is Hiring?" threads. Job posts on HN live as top-level comments,
so we search recent comments and rely on the recall-first classifier and the
keyword filter downstream to keep the relevant ones.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterator

from ..models import RawItem
from .base import Collector, FetchContext, HttpClient
from .registry import register

_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


def _strip_html(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


@register("hn")
class HackerNewsCollector(Collector):
    type = "hn"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        # A jobs-only context (the Who-Is-Hiring thread) — raise the classifier
        # prior so short comments are still kept (FR-8).
        self.query = config.get("query", "")

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        params = "tags=comment&hitsPerPage=100"
        if self.query:
            from urllib.parse import quote

            params += f"&query={quote(self.query)}"
        if ctx.since:
            params += f"&numericFilters=created_at_i>{int(ctx.since.timestamp())}"
        url = f"{_SEARCH_URL}?{params}"
        status, body = self.http.get(url)
        yield from self.parse(body, self.name)

    @staticmethod
    def parse(body: bytes, source_name: str) -> Iterator[RawItem]:
        data = json.loads(body)
        for hit in data.get("hits", []):
            text = _strip_html(hit.get("comment_text") or hit.get("story_text") or "")
            if not text:
                continue
            oid = str(hit.get("objectID"))
            ts = hit.get("created_at_i")
            posted = (
                datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
            )
            yield RawItem(
                source=source_name,
                external_id=oid,
                raw_text=text,
                url=f"https://news.ycombinator.com/item?id={oid}",
                raw_json=json.dumps(hit),
                posted_at=posted,
            )
