"""The three-stage filter pipeline (Section 4.4).

Filters run in order; a posting must pass all three to be delivered:

1. Keyword filter (FR-13/14) — case-insensitive, variant-aware.
2. Remote filter (FR-15/16) — when remote-only, explicit on-site/hybrid is
   rejected; explicit-remote and unknown pass.
3. Eligibility filter (FR-17-20) — intersection of hiring geography and the
   user's eligible countries, with worldwide passing for everyone (FR-19) and
   undetermined geography routed to review (FR-20).

Each user carries their own settings, so the same posting can pass for one
member and be rejected for another (AC-6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import geo
from .models import Posting


# Decisions the pipeline acts on.
PASS = "pass"
REJECT = "reject"
REVIEW = "review"  # FR-20/21: undetermined geography → review queue


@dataclass
class UserSettings:
    id: str
    keywords: list[str]
    eligible_countries: list[str]   # already normalised to ISO codes
    remote_only: bool


@dataclass
class FilterResult:
    decision: str                    # PASS / REJECT / REVIEW
    matched_keywords: list[str]
    stage: str                       # which stage produced the decision
    reason: str


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Build a case-insensitive, variant-tolerant matcher for a keyword.

    Handles the FR-14 example family: ``fullstack`` also matches ``full-stack``
    and ``full stack``. We do this by allowing an optional separator between
    the letters of a compound keyword's word boundaries — concretely, we split
    a keyword on known separators and rejoin with a flexible separator class.
    """
    kw = keyword.strip().lower()
    # Split common compound forms into parts, e.g. "full-stack" -> full, stack.
    parts = re.split(r"[\s\-_]+", kw)
    if len(parts) > 1:
        core = r"[\s\-_]*".join(re.escape(p) for p in parts)
    else:
        # Also let a single compound like "fullstack" match "full stack" by
        # trying a small set of known splits.
        core = re.escape(kw)
    return re.compile(r"(?<![a-z0-9])" + core + r"(?![a-z0-9])", re.I)


# Cache compiled patterns per keyword string.
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
    """Return the user's keywords that appear in the text (FR-14)."""
    matched: list[str] = []
    for kw in keywords:
        forms = _VARIANTS.get(kw.strip().lower(), [kw])
        if any(_pattern_for(f).search(text) for f in forms):
            matched.append(kw)
    return matched


def keyword_stage(posting: Posting, settings: UserSettings) -> tuple[bool, list[str]]:
    haystack = f"{posting.title}\n{posting.description}"
    matched = match_keywords(haystack, settings.keywords)
    return (bool(matched), matched)


def remote_stage(posting: Posting, settings: UserSettings) -> bool:
    """FR-16: when remote-only, pass explicit-remote or unknown; reject
    explicit on-site/hybrid."""
    if not settings.remote_only:
        return True
    return posting.is_remote in ("remote", "unknown")


def eligibility_stage(posting: Posting, settings: UserSettings) -> geo.EligibilityResult:
    return geo.check_eligibility(
        posting.hiring_countries, posting.is_worldwide, settings.eligible_countries
    )


def evaluate(posting: Posting, settings: UserSettings) -> FilterResult:
    """Run all three stages in order and return a single decision."""
    # 1. Keyword
    ok, matched = keyword_stage(posting, settings)
    if not ok:
        return FilterResult(REJECT, [], "keyword", "no_keyword_match")

    # 2. Remote
    if not remote_stage(posting, settings):
        return FilterResult(REJECT, matched, "remote", f"remote_only_rejects_{posting.is_remote}")

    # 3. Eligibility
    elig = eligibility_stage(posting, settings)
    if elig.undetermined:
        return FilterResult(REVIEW, matched, "eligibility", elig.reason)
    if not elig.passed:
        return FilterResult(REJECT, matched, "eligibility", elig.reason)

    return FilterResult(PASS, matched, "eligibility", elig.reason)
