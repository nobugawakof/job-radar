"""Region matching for the region filter.

The user picks one or more *regions of interest* — a country, a city, a bloc,
whatever they type ("Hong Kong", "Malaysia", "EU", "Berlin"). A posting passes
the region filter when it is remote AND either:

* it hires from anywhere on Earth (see :func:`worldwide`), or
* its stated location names one of the user's regions.

This is intentionally text-based rather than a full geocoder: it matches the
region strings the user actually typed against the posting's location (or, if no
location was extracted, the start of its text). That keeps it simple, needs no
data tables to maintain, and handles cities and regions, not just countries.
"""

from __future__ import annotations

import re

# ----------------------------------------------------------- worldwide
# "Is this job open to people anywhere on Earth?" Getting this wrong in the
# *yes* direction lets region-locked jobs past your region filter, so words are
# weighed by how much they actually say about hiring:
#
# * STRONG phrases mean hire-from-anywhere on their own.
# * WEAK words (global, international, anywhere, 全球) usually describe the
#   company ("a global fintech", "全球领先"), so they only count inside the
#   job's location field or right next to "remote" ("Remote (Global)").
# * A RESTRICTION beats both: "anywhere in the US", "Remote within Canada",
#   "US only", "must be based in the UK", "国内远程".
STRONG_WORLDWIDE = [
    "worldwide", "world wide", "world-wide", "anywhere in the world", "anywhere on earth",
    "anywhere on the planet", "location independent", "location-independent",
    "work from anywhere", "remote from anywhere", "hire from anywhere", "hiring from anywhere",
    "hire globally", "hiring globally", "we hire anywhere", "open to candidates globally",
    "open to candidates anywhere", "any country", "any location", "all countries",
    "any timezone", "any time zone", "all timezones", "all time zones",
    "全球远程", "全球招聘", "全球范围", "全球均可", "全球可远程", "远程（全球）", "远程(全球)",
    "不限地区", "地点不限", "工作地点不限", "不限国家", "不限地点",
]
_STRONG_RE = re.compile("|".join(re.escape(t) for t in STRONG_WORLDWIDE), re.I)
# "Worldwide" also describes the business ("trusted by companies worldwide");
# that use says nothing about where you can work from.
_BUSINESS_BEFORE = re.compile(
    r"\b(?:customers?|clients?|users?|companies|businesses|brands|merchants|teams|offices|people|"
    r"developers|millions|thousands|countries|markets|used|trusted|served|serving|sold|shipped|"
    r"presence|operations|leader|leading|known)\b[\w\s,]{0,20}$", re.I)


def _strong_hit(text: str) -> bool:
    for m in _STRONG_RE.finditer(text):
        if m.group(0).lower().replace("-", " ").startswith("world") and \
                _BUSINESS_BEFORE.search(text[max(0, m.start() - 40):m.start()]):
            continue  # "...customers worldwide": about the company, not the job
        return True
    return False

_WEAK = r"(?:global|globally|international|internationally|anywhere)"
# "Remote (Global)", "Remote - anywhere", "remote, international." — the weak
# word must end the phrase, not start a description ("remote, global team").
_WEAK_AFTER_REMOTE = re.compile(
    r"\bremote\b[\s\-–—:,(/|]{0,4}" + _WEAK + r"\b(?=\s*(?:$|[)\]|,.;:/!\n]|\b(?:and|or|role|position|job|opportunity)\b))",
    re.I)
# "Globally remote", "anywhere-remote" (adverbs only: "global remote company" is description).
_WEAK_BEFORE_REMOTE = re.compile(r"\b(?:globally|anywhere)[\s\-]{1,2}remote\b", re.I)
# A short location field that is just a weak word: "Global", "Remote / International".
_WEAK_LOCATION = re.compile(r"^\W*(?:remote\W*)?" + _WEAK + r"\W*(?:remote)?\W*$", re.I)

# Places that turn "anywhere"/"remote" into a regional job. Also used for
# lower-case posts, where a capital letter can't mark a place name.
REGION_WORDS = [
    "us", "usa", "u.s", "u.s.a", "united states", "america", "north america", "south america",
    "latin america", "latam", "canada", "mexico", "brazil", "argentina", "colombia", "chile",
    "uk", "u.k", "united kingdom", "britain", "england", "ireland", "europe", "eu", "eea", "emea",
    "germany", "france", "spain", "portugal", "italy", "netherlands", "belgium", "switzerland",
    "austria", "poland", "sweden", "norway", "denmark", "finland", "czechia", "romania", "ukraine",
    "apac", "asia", "southeast asia", "sea", "india", "china", "mainland china", "hong kong",
    "taiwan", "japan", "korea", "singapore", "malaysia", "indonesia", "philippines", "vietnam",
    "thailand", "australia", "new zealand", "anz", "africa", "nigeria", "kenya", "south africa",
    "egypt", "middle east", "israel", "uae", "dubai", "turkey", "pakistan", "bangladesh",
]
_REGION_LOWER = {r.replace(".", "") for r in REGION_WORDS}
_PLACE = r"(?:the\s+)?(\w[\w.'\-]*(?:\s+\w[\w.'\-]*)?)"
_NOT_PLACES = {"world", "globe", "earth", "planet", "timezone", "time", "your", "our", "a", "any"}
# (pattern, place must be a known region name). "Remote in X" is ambiguous
# ("remote in English", "remote in a Slack-first team"), so only known places
# count there; the other phrasings are inherently geographic, so a capitalised
# name ("anywhere in Brazil", "based in Lyon") is enough.
_RESTRICTION_PATTERNS = [
    (re.compile(r"\banywhere\s+(?:in|within|across|throughout|inside)\s+" + _PLACE, re.I), False),
    (re.compile(r"\bremote\s*(?:\(|-|–|,)?\s*(?:in|within|across|inside|from)\s+" + _PLACE, re.I), True),
    (re.compile(r"\b(?:must|need\s+to|needs\s+to|required\s+to|have\s+to|has\s+to|should)\s+(?:be\s+)?"
                r"(?:based|located|living|reside|residing|live|resident)\s+(?:in|within)\s+" + _PLACE, re.I), False),
    (re.compile(r"\b(?:eligible|authori[sz]ed|permitted|legally\s+able|right)\s+to\s+work\s+in\s+" + _PLACE, re.I), False),
    (re.compile(r"\bonly\s+(?:in|within|from|for)\s+" + _PLACE, re.I), False),
]
_NOT_PLACE_NAMES = {"english", "spanish", "french", "german", "mandarin", "chinese", "japanese",
                    "portuguese", "slack", "github", "remote", "person", "office"}
_REGION_ONLY = re.compile(
    r"\b(" + "|".join(re.escape(r) for r in sorted(REGION_WORDS, key=len, reverse=True)) + r")\.?"
    r"[\s\-]*(?:time\s*zones?\s+|timezones?\s+|based\s+|residents?\s+|citizens?\s+|candidates?\s+)?only\b",
    re.I)
CHINESE_RESTRICTIONS = [
    "国内远程", "仅限国内", "限国内", "只限国内", "需在国内", "需要在国内", "仅限中国",
    "限中国大陆", "仅限大陆", "限大陆", "中国大陆地区", "仅限本地",
]


def _is_place(phrase: str, known_only: bool) -> bool:
    words = phrase.lower().replace(".", "").split()
    first = words[0].rstrip("'s") if words else ""
    if not words or first in _NOT_PLACES or first in _NOT_PLACE_NAMES:
        return False
    if first in _REGION_LOWER or " ".join(words[:2]) in _REGION_LOWER:
        return True
    return not known_only and phrase[:1].isupper()


def _trim_place(phrase: str) -> str:
    """Keep a second word only when it is part of the name ("Hong Kong",
    "New Zealand", "united states"), not trailing prose ("US please")."""
    words = phrase.split()
    if len(words) >= 2 and (words[1][:1].isupper()
                            or " ".join(words[:2]).lower().replace(".", "") in _REGION_LOWER):
        return phrase.rstrip(".,;:)")
    return words[0].rstrip(".,;:)") if words else phrase


def restriction(text: str | None) -> str | None:
    """The phrase that limits where people can work from, if any — e.g.
    "anywhere in the US", "US only", "must be based in the UK", "国内远程"."""
    if not text:
        return None
    for zh in CHINESE_RESTRICTIONS:
        if zh in text:
            return zh
    m = _REGION_ONLY.search(text)
    if m:
        return m.group(0)
    for rx, known_only in _RESTRICTION_PATTERNS:
        for m in rx.finditer(text):
            if _is_place(m.group(1), known_only):
                return text[m.start():m.start(1) + len(_trim_place(m.group(1)))]
    return None


def worldwide(location: str | None, text: str | None) -> bool:
    """True if the job hires from anywhere on Earth (see the rules above)."""
    if restriction(location) or restriction(text):
        return False
    loc = (location or "").strip()
    if loc and (_strong_hit(loc) or _WEAK_LOCATION.match(loc)):
        return True
    body = text or ""
    return bool(_strong_hit(body) or _WEAK_AFTER_REMOTE.search(body)
                or _WEAK_BEFORE_REMOTE.search(body))


def is_worldwide(text: str | None) -> bool:
    """Single-string form of :func:`worldwide` (kept for older callers)."""
    return worldwide(None, text)


# ------------------------------------------------------------- regions
_SAYS_WORLDWIDE = {"worldwide", "global", "anywhere", "remote", "全球", "不限"}


def _term_in(term: str, text: str) -> bool:
    return bool(re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])", text.lower()))


def region_check(location: str | None, text: str | None, regions: list[str]) -> str:
    """Does a posting's hiring geography fit the user's regions?

    Returns ``"match"``, ``"no_match"`` or ``"unknown"`` (the posting names no
    place at all). Places are compared as *sets of countries* (see
    :mod:`jobradar.places`), so "Germany" fits "EU only" and "Malaysia" fits
    "APAC"; anything the gazetteer doesn't know ("Bay Area North") is matched
    as plain text. Evidence is taken from the most specific source that names
    a place: a restriction phrase ("US only", 国内远程), then the location
    field, then the start of the post.
    """
    from . import places

    user_codes: set[str] = set()
    user_terms: list[str] = []
    for r in regions:
        r = r.strip()
        if not r or r.lower() in _SAYS_WORLDWIDE:
            continue  # "Worldwide" as a region = hire-from-anywhere jobs, handled upstream
        user_codes |= places.resolve(r)
        user_terms.append(r)
    evidence_list = [(restriction(location), True), (restriction(text), True),
                     (location, False), ((text or "")[:600], False)]
    for evidence, geographic in evidence_list:
        if not evidence:
            continue
        codes = places.find(evidence, any_case=geographic)
        if codes & user_codes or any(_term_in(t, evidence) for t in user_terms):
            return "match"
        if codes:
            return "no_match"
    return "unknown"


def region_matches(location: str | None, text: str | None, regions: list[str]) -> bool:
    """True if the posting names one of the user's regions (kept for older
    callers; "location not stated" counts as no match here)."""
    if not regions:
        return True
    return region_check(location, text, regions) == "match"
