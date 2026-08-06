"""Reddit collector (Tier A).

Uses the official Data API under an approved OAuth application (D-1). Access is
free at 100 QPM for non-commercial use (C-5, NFR-3); the shared HttpClient's
per-request throttle keeps us well under that limit with margin.

Credentials come from the environment (DR-6), never the config file or the DB:
``JOBRADAR_REDDIT_CLIENT_ID`` / ``JOBRADAR_REDDIT_CLIENT_SECRET``. A source's
config names the subreddit(s) to read.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

from ..models import RawItem
from .base import Collector, CollectorError, FetchContext, HttpClient
from .registry import register

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


@register("reddit")
class RedditCollector(Collector):
    type = "reddit"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        self.subreddit = config["subreddit"]
        self.listing = config.get("listing", "new")  # new / hot
        self._token: str | None = None

    def _get_token(self, user_agent: str) -> str:
        # Prefer credentials from the config file; fall back to env vars.
        cid = self.config.get("reddit_client_id") or os.environ.get("JOBRADAR_REDDIT_CLIENT_ID")
        secret = self.config.get("reddit_client_secret") or os.environ.get("JOBRADAR_REDDIT_CLIENT_SECRET")
        if not cid or not secret:
            raise CollectorError(
                "Reddit credentials missing: set reddit_client_id and "
                "reddit_client_secret in config.toml"
            )
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
        req.add_header("Authorization", f"Basic {auth}")
        req.add_header("User-Agent", user_agent)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
        except Exception as e:  # noqa: BLE001 - normalise to CollectorError (SR-3)
            raise CollectorError(f"Reddit token request failed: {e}") from e
        token = payload.get("access_token")
        if not token:
            raise CollectorError("Reddit token response missing access_token")
        return token

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        token = self._get_token(ctx.user_agent)
        url = f"https://oauth.reddit.com/r/{self.subreddit}/{self.listing}?limit=100&raw_json=1"
        status, body = self.http.get(url, headers={"Authorization": f"Bearer {token}"})
        for item in self.parse(body, self.name):
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item

    @staticmethod
    def parse(body: bytes, source_name: str) -> Iterator[RawItem]:
        data = json.loads(body)
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            title = (d.get("title") or "").strip()
            selftext = (d.get("selftext") or "").strip()
            text = "\n".join(filter(None, [title, selftext])) or title
            if not text:
                continue
            created = d.get("created_utc")
            posted = (
                datetime.fromtimestamp(int(created), tz=timezone.utc) if created else None
            )
            permalink = d.get("permalink") or ""
            url = f"https://www.reddit.com{permalink}" if permalink else d.get("url")
            yield RawItem(
                source=source_name,
                external_id=str(d.get("id") or d.get("name")),
                raw_text=text,
                url=url,
                raw_json=json.dumps(d),
                posted_at=posted,
                title_hint=title or None,
                location_hint=None,
            )
