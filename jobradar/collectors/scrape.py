"""Generic best-effort scrape collector (Tier B).

Tier B is publicly-reachable HTML fetched over plain HTTPS and parsed. It is
expected to break, and a break is *not* a defect (SRS 3.1). This collector
therefore does the minimum honest thing:

* fetches at the polite shared rate (C-4, SR-6);
* identifies itself honestly and never rotates identity or uses proxies
  (SR-5) — it inherits the shared HttpClient which enforces this;
* does not attempt anything behind auth walls, CAPTCHAs, or request signing
  (C-3) — if a page needs any of that, this collector simply fails, and the
  pipeline's Tier-B auto-disable (SR-4) takes over;
* extracts candidate post blocks with a configurable CSS-ish selector
  expressed as a regex in config (NFR-14), keeping the fragility contained to
  this one class (NFR-15).

`robots.txt` is consulted before fetching (C-4).
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.robotparser
from datetime import datetime, timezone
from typing import Any, Iterator

from ..models import RawItem
from .base import Collector, CollectorError, FetchContext, HttpClient, SourceBlocked
from .registry import register

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _text_of(html_fragment: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html_fragment)).strip()


@register("scrape")
class ScrapeCollector(Collector):
    type = "scrape"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier  # expected "B"
        self.config = config
        self.http = http
        self.url = config["url"]
        # A regex that captures each candidate posting block from the page.
        # Expressed as data so the scraper can be re-tuned without code (NFR-14).
        self.block_pattern = config.get("block_pattern", r"<article[^>]*>(.*?)</article>")

    def _robots_allows(self, ctx: FetchContext) -> bool:
        parts = urllib.parse.urlparse(self.url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            status, body = self.http.get(robots_url, accept="text/plain")
            rp.parse(body.decode("utf-8", "replace").splitlines())
        except CollectorError:
            # No robots.txt reachable → default to allowed, but stay polite.
            return True
        return rp.can_fetch(ctx.user_agent, self.url)

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        if not self._robots_allows(ctx):
            # C-4 / NFR-18: robots disallow is treated as a block.
            raise SourceBlocked(f"robots.txt disallows {self.url}")
        status, body = self.http.get(self.url, accept="text/html")
        yield from self.parse(body, self.name, self.block_pattern, ctx.now)

    @staticmethod
    def parse(
        body: bytes, source_name: str, block_pattern: str, now: datetime | None = None
    ) -> Iterator[RawItem]:
        html = body.decode("utf-8", "replace")
        blocks = re.findall(block_pattern, html, re.S | re.I)
        now = now or datetime.now(timezone.utc)
        for i, block in enumerate(blocks):
            text = _text_of(block)
            if not text:
                continue
            # Best-effort application link.
            m = re.search(r'href=["\'](https?://[^"\']+)["\']', block, re.I)
            link = m.group(1) if m else None
            # No reliable timestamp on a scraped page; use collection time and a
            # stable content-based id so re-scrapes dedupe (FR-6 idempotency).
            import hashlib

            ext_id = hashlib.sha1(text.encode()).hexdigest()[:16]
            yield RawItem(
                source=source_name,
                external_id=ext_id,
                raw_text=text,
                url=link,
                raw_json=json.dumps({"block_index": i}),
                posted_at=None,  # unknown → pipeline uses collection time (Section 5)
            )
