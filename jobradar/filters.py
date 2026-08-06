"""Filtering — keyword then remote.

A posting must pass both to be sent:

1. Keyword filter — case-insensitive, variant-aware (``fullstack`` also matches
   ``full-stack`` / ``full stack``). At least one keyword must match.
2. Remote filter — when remote-only is on, a posting passes if it is explicitly
   remote or if its work arrangement is unknown; explicit on-site/hybrid is
   rejected.

Eligibility/country filtering and the review queue from the original spec were
dropped for the Telegram-only build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Posting


PASS = "pass"
REJECT = "reject"


@dataclass
class Settings:
    keywords: list[str]
    remote_only: bool


@dataclass
class FilterResult:
    decision: str            # PASS / REJECT
    matched_keywords: list[str]
    stage: str
    reason: str


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    kw = keyword.strip().lower()
    parts = re.split(r"[\s\-_]+", kw)
    core = r"[\s\-_]*".join(re.escape(p) for p in parts) if len(parts) > 1 else re.escape(kw)
    return re.compile(r"(?<![a-z0-9])" + core + r"(?![a-z0-9])", re.I)


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _pattern_for(keyword: str) -> re.Pattern[str]:
    p = _PATTERN_CACHE.get(keyword)
    if p is None:
        p = _keyword_pattern(keyword)
        _PATTERN_CACHE[keyword] = p
    return p


# Known variant expansions so "fullstack" matches "full stack" and vice-versa.
_VARIANTS: dict[str, list[str]] = {
    "fullstack": ["fullstack", "full-stack", "full stack"],
    "frontend": ["frontend", "front-end", "front end"],
    "backend": ["backend", "back-end", "back end"],
    "web3": ["web3", "web 3"],
    "web2": ["web2", "web 2"],
}


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    matched: list[str] = []
    for kw in keywords:
        forms = _VARIANTS.get(kw.strip().lower(), [kw])
        if any(_pattern_for(f).search(text) for f in forms):
            matched.append(kw)
    return matched


def keyword_stage(posting: Posting, settings: Settings) -> tuple[bool, list[str]]:
    haystack = f"{posting.title}\n{posting.description}"
    matched = match_keywords(haystack, settings.keywords)
    return (bool(matched), matched)


def remote_stage(posting: Posting, settings: Settings) -> bool:
    if not settings.remote_only:
        return True
    return posting.is_remote in ("remote", "unknown")


def evaluate(posting: Posting, settings: Settings) -> FilterResult:
    # An empty keyword set means "send everything" (no keyword gate).
    if settings.keywords:
        ok, matched = keyword_stage(posting, settings)
        if not ok:
            return FilterResult(REJECT, [], "keyword", "no_keyword_match")
    else:
        matched = []

    if not remote_stage(posting, settings):
        return FilterResult(REJECT, matched, "remote", f"remote_only_rejects_{posting.is_remote}")

    return FilterResult(PASS, matched, "remote", "ok")
