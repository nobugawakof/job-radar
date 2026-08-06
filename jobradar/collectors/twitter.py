"""Twitter / X collector.

Uses the official X API v2 recent-search endpoint with a Bearer token. Note the
free read tier was discontinued, so this **requires a paid X API plan** — the
token is billed per request. It's included because the token can be configured
in the file like any other; if you don't have a paid key, just don't add an X
source.

The token comes from ``x_bearer_token`` in config.toml (or the
``JOBRADAR_X_BEARER_TOKEN`` environment variable).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import quote

from ..models import RawItem
from ..state import parse_iso
from .base import Collector, CollectorError, FetchContext, HttpClient
from .registry import register

_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"


@register("twitter")
@register("x")
class TwitterCollector(Collector):
    type = "twitter"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        self.query = config.get("query", "(hiring OR \"we're hiring\") (developer OR engineer) -is:retweet")

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        token = self.config.get("x_bearer_token") or os.environ.get("JOBRADAR_X_BEARER_TOKEN")
        if not token:
            raise CollectorError(
                "X (Twitter) bearer token missing: set x_bearer_token in config.toml "
                "(requires a paid X API plan)"
            )
        url = (
            f"{_SEARCH_URL}?query={quote(self.query)}&max_results=100"
            "&tweet.fields=created_at,author_id"
        )
        status, body = self.http.get(url, headers={"Authorization": f"Bearer {token}"})
        for item in self.parse(body, self.name):
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item

    @staticmethod
    def parse(body: bytes, source_name: str) -> Iterator[RawItem]:
        data = json.loads(body)
        if "errors" in data and not data.get("data"):
            raise CollectorError(f"X API error: {data['errors']}")
        for tweet in data.get("data", []):
            text = (tweet.get("text") or "").strip()
            if not text:
                continue
            tid = str(tweet.get("id"))
            posted = parse_iso(tweet.get("created_at"))
            yield RawItem(
                source=source_name,
                external_id=tid,
                raw_text=text,
                url=f"https://twitter.com/i/web/status/{tid}",
                raw_json=json.dumps(tweet),
                posted_at=posted,
            )
