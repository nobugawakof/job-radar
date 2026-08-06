"""Field extraction.

For each detected posting the system extracts the Section 5 fields (FR-10).
Two rules dominate the design:

* FR-11 — any field that cannot be extracted with confidence is recorded as
  *unknown*, never guessed.
* FR-12 — the original, unmodified post text is always stored regardless of
  extraction success; it is the record of truth and the input to Phase 2.

Extraction is expressed with tunable regex/data tables (NFR-14).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import geo


# --- Remote / work-arrangement detection (feeds FR-16) --------------------
_REMOTE_RE = re.compile(
    r"\b(fully[ -]?remote|100%\s*remote|remote[- ]?first|remote|work from home|wfh|distributed team)\b",
    re.I,
)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[- ]?site|onsite|in[- ]?office|in person|relocat)\w*\b", re.I)


def detect_remote(text: str) -> str:
    """Return one of remote/hybrid/onsite/unknown.

    Precedence matters: an explicit "hybrid" or "on-site" qualifies a "remote"
    mention (e.g. "remote-friendly but hybrid preferred"). Unknown is a valid,
    non-guessed outcome (FR-11) and passes the remote filter (FR-16).
    """
    if not text:
        return "unknown"
    hybrid = bool(_HYBRID_RE.search(text))
    onsite = bool(_ONSITE_RE.search(text))
    remote = bool(_REMOTE_RE.search(text))
    if remote and not hybrid and not onsite:
        return "remote"
    if hybrid:
        return "hybrid"
    if onsite and not remote:
        return "onsite"
    if remote:
        # Remote mentioned alongside onsite language → ambiguous, keep it.
        return "remote"
    return "unknown"


# --- Title extraction ------------------------------------------------------
_TITLE_STRIP_RE = re.compile(r"^\s*(\[[^\]]*\]\s*|hiring[:\- ]+|we'?re hiring[:\- ]*)", re.I)
# A first line that is really a structured field label, not a title.
_FIELD_LABEL_RE = re.compile(
    r"^(location|remote|onsite|hybrid|willing to relocate|technologies|tech stack|"
    r"salary|compensation|comp|visa|url|website|apply|email)\b\s*[:\-]",
    re.I,
)


def _structured_field(text: str, names: str) -> str | None:
    """Pull a labelled field's value, e.g. Company: / Role: / Position:."""
    m = re.search(rf"^\s*(?:{names})\s*[:\-]\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip() if m else None


def extract_title(text: str, fallback_max: int = 120) -> str:
    """Best-effort role title.

    Handles the common "Who Is Hiring" header shape — ``Company | Role | …`` —
    by keeping the header line, and the field-structured shape by combining the
    Company and Role fields. Falls back to a one-line summary otherwise.
    """
    first = ""
    for line in text.splitlines():
        s = line.strip()
        if s:
            first = _TITLE_STRIP_RE.sub("", s).strip()
            break
    if not first:
        flat = " ".join(text.split())
        return flat[:fallback_max] if flat else "(untitled posting)"

    # A field-structured post (starts with "Location:" etc.): build the title
    # from Company / Role fields instead of the label line.
    if _FIELD_LABEL_RE.match(first):
        company = _structured_field(text, "company|employer")
        role = _structured_field(text, "role|position|title|job")
        parts = [p for p in (company, role) if p]
        if parts:
            return " — ".join(parts)[:fallback_max]

    return first[:fallback_max]


# --- Contact extraction (DR-4 personal data — minimise, never log) --------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
_APPLY_HINT_RE = re.compile(r"(apply|jobs?|careers?|greenhouse|lever|workable|ashby)", re.I)


def extract_contact(text: str) -> str | None:
    m = _EMAIL_RE.search(text)
    if m:
        return m.group(0)
    for url in _URL_RE.findall(text):
        if _APPLY_HINT_RE.search(url):
            return url
    m = _HANDLE_RE.search(text)
    if m:
        return m.group(0)
    return None


def extract_apply_url(text: str) -> str | None:
    """Normalised application URL used for deduplication (FR-28)."""
    for url in _URL_RE.findall(text):
        if _APPLY_HINT_RE.search(url):
            return _normalise_url(url)
    urls = _URL_RE.findall(text)
    return _normalise_url(urls[0]) if urls else None


def _normalise_url(url: str) -> str:
    url = url.rstrip(".,);]")
    # Drop tracking query/fragment; compare on scheme+host+path.
    url = re.split(r"[?#]", url, 1)[0]
    return url.lower().rstrip("/")


# --- Location / hiring geography ------------------------------------------
_LOCATION_LABEL_RE = re.compile(
    r"(?:location|based in|hiring in|located in|region|geo)\s*[:\-]?\s*([^\n.|]+)",
    re.I,
)
# "Remote (US)", "REMOTE - EU", "Remote: Worldwide" → capture the qualifier.
_REMOTE_QUALIFIER_RE = re.compile(
    r"remote\s*[:\-(]\s*([A-Za-z][A-Za-z ,/&\-]{1,40})\)?",
    re.I,
)


def extract_location(text: str) -> str | None:
    """Best-effort hiring location string (for display and region matching)."""
    m = _LOCATION_LABEL_RE.search(text)
    if m:
        loc = m.group(1).strip(" -")[:120]
        if loc:
            return loc
    # e.g. "Remote (US)" / "Remote: Worldwide".
    q = _REMOTE_QUALIFIER_RE.search(text)
    if q:
        val = q.group(1).strip()
        # Avoid swallowing sentence fragments like "Remote is great because…".
        if len(val.split()) <= 5 and val.lower() not in ("is", "work", "position", "role"):
            return val[:120]
    return None


# --- Salary parsing --------------------------------------------------------
_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "usd": "USD", "eur": "EUR", "gbp": "GBP"}
_PERIOD_RE = re.compile(r"\b(per\s+year|/\s*year|annually|annual|p\.?a\.?|yr|per\s+hour|/\s*hour|hourly|per\s+month|/\s*month|monthly)\b", re.I)
_AMOUNT_RE = re.compile(
    r"(?P<cur>[$€£])?\s?(?P<a>\d{1,3}(?:[,.\s]\d{3})+|\d{2,3})\s?(?P<ka>[kK])?"
    r"(?:\s?[-–to]{1,3}\s?(?P<cur2>[$€£])?\s?(?P<b>\d{1,3}(?:[,.\s]\d{3})+|\d{2,3})\s?(?P<kb>[kK])?)?"
)


@dataclass
class Salary:
    raw: str | None = None
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: str | None = None


def parse_salary(text: str) -> Salary:
    """Parse compensation where possible; leave fields unknown otherwise (FR-11).

    ``salary_raw`` preserves what was written; the parsed fields are populated
    only when a numeric range/amount with a currency is confidently found.
    """
    sal = Salary()
    # A plausible salary snippet: a currency symbol immediately followed by a
    # number (optionally a range). Requiring the digit avoids grabbing prose
    # like "$" inside a word or a lone symbol.
    m = re.search(
        r"[$€£]\s?\d[\d.,]*\s?[kK]?(?:\s?[-–—to]{1,3}\s?[$€£]?\s?\d[\d.,]*\s?[kK]?)?",
        text,
    )
    window = m.group(0) if m else None
    if window is None:
        # A "Salary:/Compensation:" label whose value contains a digit.
        lab = re.search(r"(?:salary|compensation|comp|pay)\s*[:\-]?\s*([^\n]{1,60})", text, re.I)
        if lab and re.search(r"\d", lab.group(1)):
            window = lab.group(1)
    if not window:
        return sal

    sal.raw = window.strip()

    cur = None
    for sym, code in _CURRENCY.items():
        if sym in window.lower():
            cur = code
            break
    sal.currency = cur

    pm = _PERIOD_RE.search(window) or _PERIOD_RE.search(text)
    if pm:
        p = pm.group(0).lower()
        if "hour" in p:
            sal.period = "hour"
        elif "month" in p:
            sal.period = "month"
        else:
            sal.period = "year"

    am = _AMOUNT_RE.search(window)
    if am and am.group("a"):
        lo = _to_number(am.group("a"), am.group("ka"))
        hi = _to_number(am.group("b"), am.group("kb")) if am.group("b") else None
        if lo is not None:
            sal.min = lo
            sal.max = hi if hi is not None else lo
            if not sal.currency and (am.group("cur") or am.group("cur2")):
                sym = am.group("cur") or am.group("cur2")
                sal.currency = _CURRENCY.get(sym)
    return sal


def _to_number(digits: str | None, k: str | None) -> float | None:
    if not digits:
        return None
    n = float(re.sub(r"[,.\s]", "", digits)) if len(re.sub(r"[^\d]", "", digits)) > 3 else float(re.sub(r"[^\d]", "", digits))
    if k:
        n *= 1000
    return n


# --- Full extraction -------------------------------------------------------
@dataclass
class Extracted:
    title: str
    description: str
    contact: str | None
    location: str | None
    is_remote: str
    is_worldwide: bool = False
    apply_url: str | None = None
    salary: Salary = field(default_factory=Salary)


def extract(text: str, *, given_title: str | None = None, given_location: str | None = None) -> Extracted:
    """Extract posting fields from a post's text.

    ``given_*`` let a collector pass structured hints it already has (e.g. a
    Reddit post title, an RSS item location) so we don't re-derive them. The
    original text is preserved unmodified as ``description``.
    """
    description = text  # unmodified — the record of truth
    title = (given_title or "").strip() or extract_title(text)
    location = (given_location or "").strip() or extract_location(text)
    worldwide = geo.is_worldwide(location) or geo.is_worldwide(text)

    return Extracted(
        title=title,
        description=description,
        contact=extract_contact(text),
        location=location,
        is_remote=detect_remote(text),
        is_worldwide=worldwide,
        apply_url=extract_apply_url(text),
        salary=parse_salary(text),
    )
