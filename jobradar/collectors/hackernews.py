"""Hacker News collector (Tier A).

Reads the monthly **"Ask HN: Who Is Hiring?"** thread — the one posted by the
``whoishiring`` account each month — and yields its *top-level comments*, which
by convention are the job posts (``Company | Role | Location | Remote | …``).

This is deliberately NOT a search across all of Hacker News: doing that pulls in
ordinary discussion comments and, worse, posts from the sibling "Who wants to be
hired?" thread (job *seekers*, which start with "Location: …"). Targeting the
hiring thread's direct replies is what keeps the results actual job openings and
their titles/locations clean.

Uses the free Algolia HN Search API. Two cheap requests per run: find the
latest hiring story, then fetch its comments.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote

from ..models import RawItem
from .base import Collector, CollectorError, FetchContext, HttpClient
from .registry import register

_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH = "https://hn.algolia.com/api/v1/search"

# Titles of the three monthly threads; we want the hiring one, not the others.
_HIRING_RE = re.compile(r"\bwho\s+is\s+hiring\b", re.I)
_EXCLUDE_RE = re.compile(r"who\s+wants\s+to\s+be\s+hired|freelancer", re.I)


def _strip_html(s: str) -> str:
    # Convert <p> to newlines first so the header line survives as line 1.
    s = re.sub(r"<p>", "\n", s or "", flags=re.I)
    return html.unescape(_TAG_RE.sub("", s)).strip()


@register("hn")
class HackerNewsCollector(Collector):
    type = "hn"

    def __init__(self, *, name: str, tier: str, config: dict[str, Any], http: HttpClient):
        self.name = name
        self.tier = tier
        self.config = config
        self.http = http
        # Optional extra keyword to narrow the thread's comments server-side.
        self.query = config.get("query", "")

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        story_id = self._latest_hiring_story()
        if story_id is None:
            raise CollectorError("could not find a current 'Who is hiring?' thread")

        # Fetch the whole current thread rather than a recent-time window: the
        # thread is monthly, so a 72h window would miss most of the month's
        # postings. The sent-fingerprint dedup stops anything being re-sent, so
        # returning everything each run is safe.
        params = [f"tags=comment,story_{story_id}", "hitsPerPage=1000"]
        if self.query:
            params.append(f"query={quote(self.query)}")
        status, body = self.http.get(f"{_SEARCH}_by_date?{'&'.join(params)}")
        yield from self._parse_comments(body, story_id, self.name)

    def _latest_hiring_story(self) -> int | None:
        url = f"{_SEARCH}?tags=story,author_whoishiring&hitsPerPage=10"
        status, body = self.http.get(url)
        data = json.loads(body)
        best: tuple[int, int] | None = None  # (created_at_i, id)
        for hit in data.get("hits", []):
            title = hit.get("title") or ""
            if not _HIRING_RE.search(title) or _EXCLUDE_RE.search(title):
                continue
            ts = int(hit.get("created_at_i") or 0)
            oid = int(hit.get("objectID"))
            if best is None or ts > best[0]:
                best = (ts, oid)
        return best[1] if best else None

    @staticmethod
    def _parse_comments(body: bytes, story_id: int, source_name: str) -> Iterator[RawItem]:
        data = json.loads(body)
        for hit in data.get("hits", []):
            # Only top-level comments (direct children of the story) are the job
            # posts; nested replies are discussion.
            if int(hit.get("parent_id") or 0) != story_id:
                continue
            text = _strip_html(hit.get("comment_text") or "")
            if not text:
                continue
            oid = str(hit.get("objectID"))
            ts = hit.get("created_at_i")
            posted = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
            yield RawItem(
                source=source_name,
                external_id=oid,
                raw_text=text,
                url=f"https://news.ycombinator.com/item?id={oid}",
                raw_json=json.dumps(hit),
                posted_at=posted,
            )
