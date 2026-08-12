"""RSS / Atom feed collector (Tier A).

Any source that publishes a feed can be monitored by pointing this collector at
its URL. Parsing uses the standard-library XML parser; both RSS ``<item>`` and
Atom ``<entry>`` shapes are handled.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from ..models import RawItem
from .base import Collector, FetchContext, HttpClient
from .registry import register

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str | None) -> str:
    if not s:
        return ""
    # Strip tags, then decode HTML entities. Feeds like Reddit's double-escape
    # their HTML, so the text arrives as "I&#39;m a freelance &amp; ..." — decode
    # it so messages read cleanly AND the classifier can see the real words.
    return html.unescape(_TAG_RE.sub("", s)).strip()


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register("rss")
class RssCollector(Collector):
    type = "rss"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        self.url = config["url"]

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        status, body = self.http.get(self.url, accept="application/rss+xml, application/xml, text/xml")
        for item in self.parse(body, self.name):
            if ctx.since and item.posted_at and item.posted_at <= ctx.since:
                continue
            yield item

    @staticmethod
    def parse(body: bytes, source_name: str) -> Iterator[RawItem]:
        root = ET.fromstring(body)
        # Collect both RSS items and Atom entries.
        nodes = [n for n in root.iter() if _localname(n.tag) in ("item", "entry")]
        for node in nodes:
            fields: dict[str, str] = {}
            link = ""
            for child in node:
                ln = _localname(child.tag)
                if ln == "link":
                    href = child.get("href")
                    link = href or (child.text or "").strip() or link
                else:
                    fields[ln] = (child.text or "")
            title = _clean(fields.get("title"))
            summary = _clean(fields.get("description") or fields.get("summary") or fields.get("content"))
            text = "\n".join(filter(None, [title, summary])) or title
            if not text:
                continue
            guid = (fields.get("guid") or fields.get("id") or link or title).strip()
            posted = _parse_date(fields.get("pubDate") or fields.get("published") or fields.get("updated"))
            yield RawItem(
                source=source_name,
                external_id=guid,
                raw_text=text,
                url=link or None,
                raw_json=json.dumps({"title": title, "summary": summary, "link": link}),
                posted_at=posted,
                title_hint=title or None,
            )
