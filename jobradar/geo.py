"""Region matching for the region filter.

The user picks one or more *regions of interest* — a country, a city, a bloc,
whatever they type ("Hong Kong", "Malaysia", "EU", "Berlin"). A posting passes
the region filter when it is remote AND either:

* it hires from anywhere on Earth (worldwide / global / "remote anywhere"), or
* its stated location names one of the user's regions.

This is intentionally text-based rather than a full geocoder: it matches the
region strings the user actually typed against the posting's location (or, if no
location was extracted, the start of its text). That keeps it simple, needs no
data tables to maintain, and handles cities and regions, not just countries.
"""

from __future__ import annotations

import re

# Phrases that mean "hire from anywhere on Earth" (English + Chinese).
WORLDWIDE_TERMS = [
    "worldwide", "world wide", "world-wide", "global", "globally",
    "anywhere in the world", "remote anywhere", "anywhere remote",
    "work from anywhere", "location independent", "fully remote worldwide",
    "remote (worldwide)", "remote - worldwide", "remote, worldwide",
    "anywhere", "any location", "any country", "international",
    "全球远程", "全球招聘", "全球", "不限地区", "地点不限", "工作地点不限", "全球范围",
]
_WORLDWIDE_RE = re.compile("|".join(re.escape(t) for t in WORLDWIDE_TERMS), re.I)


def is_worldwide(text: str | None) -> bool:
    if not text:
        return False
    return bool(_WORLDWIDE_RE.search(text))


def region_matches(location: str | None, text: str | None, regions: list[str]) -> bool:
    """True if any of the user's region terms appears in the posting.

    Prefers the explicitly-extracted location; falls back to the start of the
    posting text when no location was found.
    """
    if not regions:
        return True  # no region filter configured
    haystack = (location or "").strip()
    if not haystack:
        haystack = (text or "")[:400]
    haystack = haystack.lower()
    for region in regions:
        r = region.strip().lower()
        if not r:
            continue
        if re.search(r"(?<![a-z])" + re.escape(r) + r"(?![a-z])", haystack):
            return True
    return False
