"""Deduplication.

The same job posted to several sources, or reposted, is sent only once, using
*near-match* comparison of posting text and any application URL — not exact
string equality. When duplicates merge within a run, all originating sources
are shown on the single delivered message.

The near-match test combines two cheap signals:

* a normalised application URL (an exact application-URL match is a strong
  duplicate signal on its own), and
* Jaccard similarity over word shingles of the posting text.

A ``content_hash`` (a compact sorted-shingle signature) is also used as the
fingerprint stored in the JSON state file to remember what was already sent.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def shingles(text: str, n: int = 3) -> set[str]:
    """Word n-gram shingles used for near-match comparison."""
    toks = _tokens(text)
    if len(toks) < n:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i : i + n]) for i in range(len(toks) - n + 1)}


def content_hash(text: str, k: int = 16) -> str:
    """Compact signature: the k smallest shingle hashes joined.

    Two near-identical texts share most of their smallest shingle hashes, so
    the signatures collide on a shared prefix far more often than random text —
    enough to cheaply pre-filter dedup candidates.
    """
    sh = shingles(text)
    if not sh:
        return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:16]
    hashed = sorted(hashlib.sha1(s.encode()).hexdigest()[:8] for s in sh)
    return "".join(hashed[:k])


def jaccard(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def is_duplicate(
    text_a: str,
    text_b: str,
    url_a: str | None = None,
    url_b: str | None = None,
    *,
    threshold: float = 0.6,
) -> bool:
    """Decide whether two postings are the same job.

    A matching normalised application URL is decisive. Otherwise the texts must
    be sufficiently similar by shingle Jaccard.
    """
    if url_a and url_b and url_a == url_b:
        return True
    return jaccard(text_a, text_b) >= threshold
