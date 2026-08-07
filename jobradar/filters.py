"""Filtering — keyword then remote.

A posting must pass all configured stages to be sent:

1. Keyword filter — case-insensitive, variant-aware (``fullstack`` also matches
   ``full-stack`` / ``full stack``). At least one keyword must match. An empty
   keyword set disables this stage (send everything).
2. Remote filter — when remote-only is on, a posting passes if it is explicitly
   remote or if its work arrangement is unknown; explicit on-site/hybrid is
   rejected.
3. Region filter — when one or more regions of interest are configured, a
   posting passes only if it hires worldwide/anywhere OR its location names one
   of those regions. This is what makes "enter Hong Kong → get Hong Kong-remote
   and remote-worldwide jobs, not US-only ones" work. An empty region list
   disables this stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import geo
from .models import Posting


PASS = "pass"
REJECT = "reject"


@dataclass
class Settings:
    keywords: list[str]
    remote_only: bool
    regions: list[str] = field(default_factory=list)
    min_salary_usd: int = 0
    require_salary: bool = False


# Rough currency → USD factors and period → per-year factors. Deliberately
# approximate: the goal is "is this roughly above my floor", not accounting.
_USD_RATES = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66, "SGD": 0.74,
    "HKD": 0.128, "CNY": 0.14, "RMB": 0.14, "JPY": 0.0067,
}
_PERIOD_FACTOR = {"hour": 2080.0, "month": 12.0, "year": 1.0, None: 1.0}


def annual_usd(salary) -> float | None:
    """Best-effort annual-USD estimate from a parsed Salary, or None if unknown."""
    amount = salary.max or salary.min
    if amount is None:
        return None
    rate = _USD_RATES.get((salary.currency or "").upper(), 1.0)
    factor = _PERIOD_FACTOR.get(salary.period, 1.0)
    return amount * factor * rate


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


# Known variant expansions so "fullstack" matches "full stack" and vice-versa,
# and so the English keywords also match common Chinese equivalents.
_VARIANTS: dict[str, list[str]] = {
    "fullstack": ["fullstack", "full-stack", "full stack", "全栈"],
    "frontend": ["frontend", "front-end", "front end", "前端"],
    "backend": ["backend", "back-end", "back end", "后端"],
    "web3": ["web3", "web 3"],
    "web2": ["web2", "web 2"],
    "ai": ["ai", "人工智能", "机器学习", "算法"],
    "remote": ["remote", "远程"],
    "devops": ["devops", "运维"],
    "designer": ["designer", "设计师"],
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


def region_stage(posting: Posting, settings: Settings) -> bool:
    """Pass if no regions configured, or the posting is worldwide, or its
    location names one of the user's regions (match OR worldwide)."""
    if not settings.regions:
        return True
    if posting.is_worldwide:
        return True
    return geo.region_matches(posting.location, posting.description, settings.regions)


def salary_stage(posting: Posting, settings: Settings) -> bool:
    """Pass unless a salary floor is set and the posting is clearly below it.

    Postings with no parseable salary pass (we can't judge) unless
    ``require_salary`` is on, in which case they're rejected.
    """
    if not settings.min_salary_usd and not settings.require_salary:
        return True
    est = annual_usd(posting.salary)
    if est is None:
        return not settings.require_salary
    return est >= settings.min_salary_usd


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

    if not region_stage(posting, settings):
        return FilterResult(REJECT, matched, "region", "outside_regions_and_not_worldwide")

    if not salary_stage(posting, settings):
        return FilterResult(REJECT, matched, "salary", "below_min_salary")

    return FilterResult(PASS, matched, "salary", "ok")
