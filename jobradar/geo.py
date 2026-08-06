"""Geography and work-eligibility logic.

This module implements the eligibility filter's core reasoning (FR-17 to
FR-20). The subtle, load-bearing rule is FR-19: a posting whose hiring
geography is worldwide / global / "anywhere" must pass for *every* user, even
one whose eligible-country list names none of the countries in the post
(AC-3). A naive country-name match would reject exactly the remote-friendly
postings most likely to be relevant.

Country handling is deliberately lightweight: a curated alias table maps the
country names and demonyms that actually appear in job posts to ISO 3166-1
alpha-2 codes. It is data (NFR-14) and easy to extend; it is not a full
geocoder, which the SRS neither needs nor budgets for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Phrases that mean "hire from anywhere on Earth" (FR-19).
WORLDWIDE_TERMS = {
    "worldwide",
    "world wide",
    "global",
    "globally",
    "anywhere",
    "anywhere in the world",
    "remote anywhere",
    "fully remote worldwide",
    "any country",
    "any location",
    "location independent",
    "work from anywhere",
    "international",
}

# A pragmatic alias table. Keys are lower-cased names/demonyms; values are
# ISO alpha-2 codes. Regional blocs expand to their member markers so that a
# post saying "EU only" intersects a user eligible in any EU country.
_COUNTRY_ALIASES: dict[str, str] = {
    "united states": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "us": "US", "america": "US", "united states of america": "US",
    "united kingdom": "GB", "uk": "GB", "u.k.": "GB", "britain": "GB",
    "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "canada": "CA", "canadian": "CA",
    "germany": "DE", "deutschland": "DE", "german": "DE",
    "france": "FR", "french": "FR",
    "spain": "ES", "spanish": "ES",
    "portugal": "PT", "portuguese": "PT",
    "netherlands": "NL", "holland": "NL", "dutch": "NL",
    "ireland": "IE", "irish": "IE",
    "poland": "PL", "polish": "PL",
    "italy": "IT", "italian": "IT",
    "serbia": "RS", "serbian": "RS",
    "croatia": "HR", "croatian": "HR",
    "switzerland": "CH", "swiss": "CH",
    "sweden": "SE", "swedish": "SE",
    "norway": "NO", "norwegian": "NO",
    "finland": "FI", "finnish": "FI",
    "denmark": "DK", "danish": "DK",
    "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "ukraine": "UA", "ukrainian": "UA",
    "romania": "RO", "romanian": "RO",
    "bulgaria": "BG", "bulgarian": "BG",
    "greece": "GR", "greek": "GR",
    "austria": "AT", "austrian": "AT",
    "belgium": "BE", "belgian": "BE",
    "czech republic": "CZ", "czechia": "CZ",
    "australia": "AU", "australian": "AU",
    "new zealand": "NZ",
    "india": "IN", "indian": "IN",
    "singapore": "SG",
    "japan": "JP", "japanese": "JP",
    "brazil": "BR", "brazilian": "BR",
    "argentina": "AR", "mexico": "MX", "mexican": "MX",
    "united arab emirates": "AE", "uae": "AE",
    "nigeria": "NG", "nigerian": "NG",
    "south africa": "ZA",
    "israel": "IL", "israeli": "IL",
}

# Regional blocs → member ISO codes present in the alias table.
_BLOCS: dict[str, list[str]] = {
    "eu": ["DE", "FR", "ES", "PT", "NL", "IE", "PL", "IT", "HR", "SE", "FI",
           "DK", "EE", "LV", "LT", "RO", "BG", "GR", "AT", "BE", "CZ"],
    "european union": ["DE", "FR", "ES", "PT", "NL", "IE", "PL", "IT", "HR",
                       "SE", "FI", "DK", "EE", "LV", "LT", "RO", "BG", "GR",
                       "AT", "BE", "CZ"],
    "eea": ["DE", "FR", "ES", "PT", "NL", "IE", "PL", "IT", "HR", "SE", "FI",
            "DK", "EE", "LV", "LT", "RO", "BG", "GR", "AT", "BE", "CZ", "NO"],
    "emea": [],   # too broad to enumerate; treated as a region marker only
    "latam": ["BR", "AR", "MX"],
    "apac": ["AU", "NZ", "IN", "SG", "JP"],
}

_WORD_RE = re.compile(r"[a-z][a-z .]*[a-z]|[a-z]")


def normalise_country(token: str) -> str | None:
    """Map a free-text country name/demonym/code to an ISO alpha-2 code."""
    t = token.strip().lower().strip(".")
    if not t:
        return None
    if t in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[t]
    up = token.strip().upper()
    # Accept a bare ISO code the user typed directly.
    if len(up) == 2 and up.isalpha() and up in set(_COUNTRY_ALIASES.values()):
        return up
    return None


def normalise_country_list(tokens: list[str]) -> list[str]:
    """Normalise a user's eligible-country list; expands bloc names too."""
    out: list[str] = []
    for tok in tokens:
        low = tok.strip().lower()
        if low in _BLOCS:
            out.extend(_BLOCS[low])
            continue
        code = normalise_country(tok)
        if code:
            out.append(code)
    # De-duplicate, preserve order.
    seen: set[str] = set()
    result = []
    for c in out:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def is_worldwide(text: str | None) -> bool:
    """True if the text marks the hiring geography as anywhere on Earth."""
    if not text:
        return False
    low = " " + re.sub(r"[^a-z ]+", " ", text.lower()) + " "
    for term in WORLDWIDE_TERMS:
        if f" {term} " in low:
            return True
    return False


def extract_hiring_countries(text: str | None) -> list[str]:
    """Pull ISO country codes referenced in a location / hiring-geography string."""
    if not text:
        return []
    low = text.lower()
    found: list[str] = []
    # Match multi-word names first (longest keys), then single words.
    for name in sorted(_COUNTRY_ALIASES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            code = _COUNTRY_ALIASES[name]
            if code not in found:
                found.append(code)
    for bloc, members in _BLOCS.items():
        if re.search(r"\b" + re.escape(bloc) + r"\b", low):
            for code in members:
                if code not in found:
                    found.append(code)
    return found


@dataclass(frozen=True)
class EligibilityResult:
    passed: bool
    undetermined: bool          # FR-20: route to review queue
    reason: str


def check_eligibility(
    hiring_countries: list[str],
    is_worldwide_flag: bool,
    eligible_countries: list[str],
) -> EligibilityResult:
    """Decide whether a posting passes a user's eligibility filter.

    Returns ``undetermined=True`` when the hiring geography could not be
    determined at all, which the caller routes to the review queue (FR-20),
    never a silent discard.
    """
    # FR-19: worldwide passes for everyone, regardless of the user's list.
    if is_worldwide_flag:
        return EligibilityResult(True, False, "worldwide")

    if not hiring_countries:
        # FR-20: geography could not be determined.
        return EligibilityResult(False, True, "undetermined")

    # If the user has not supplied an eligibility list yet (Q-1 unanswered),
    # we cannot reject on geography; treat a determinable location as passing so
    # the member still sees results while they finish setup (NFR-11).
    if not eligible_countries:
        return EligibilityResult(True, False, "no_eligibility_configured")

    # FR-18: intersection of hiring geography and eligible countries.
    intersect = set(hiring_countries) & set(eligible_countries)
    if intersect:
        return EligibilityResult(True, False, f"eligible:{','.join(sorted(intersect))}")
    return EligibilityResult(False, False, "no_intersection")
