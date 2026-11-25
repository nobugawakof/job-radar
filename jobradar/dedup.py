"""Deduplication.

The same job posted to several sources, or reposted, is sent only once, using
*near-match* comparison of posting text and any application URL — not exact
string equality. When duplicates merge within a run, all originating sources
are shown on the single delivered message.

The near-match test combines two cheap signals:

* a normalised application URL (an exact application-URL match is a strong
  duplicate signal on its own), and
* Jaccard similarity over shingles of the posting text.

Shingles are built per script:

* **Latin text** — word 3-grams (``"senior python engineer"``).
* **Chinese / Japanese / Korean** — overlapping character bigrams
  (``"后端工程师"`` → 后端 端工 工程 程师). CJK has no spaces between words, so
  word-based shingling sees nothing in it; bigrams are the standard unit for
  near-duplicate detection in CJK text.

Text is NFKC-normalised first so full-width letters and digits (``ＪＡＶＡ``,
``２５Ｋ``), common in Chinese posts, compare equal to their ASCII forms.

A ``content_hash`` (a compact sorted-shingle signature) is also used as the
fingerprint stored in the JSON state file to remember what was already sent.
For Latin-only text it is byte-for-byte what earlier versions computed, so
existing state files keep working.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache

# CJK Unified Ideographs (+ Extension A, compatibility), Japanese kana, Hangul.
_CJK = "㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_TOKEN_RE = re.compile(rf"[a-z0-9]+|[{_CJK}]+")
_CJK_RE = re.compile(f"[{_CJK}]")
_LEGACY_TOKEN_RE = re.compile(r"[a-z0-9]+")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def _word_shingles(words: list[str], n: int) -> set[str]:
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


@lru_cache(maxsize=4096)
def _shingles_cached(text: str, n: int) -> frozenset[str]:
    words: list[str] = []
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(_normalise(text)):
        if tok.isascii():
            words.append(tok)
        elif len(tok) == 1:
            out.add(tok)
        else:
            out.update(tok[i : i + 2] for i in range(len(tok) - 1))
    out |= _word_shingles(words, n)
    return frozenset(out)


def shingles(text: str, n: int = 3) -> set[str]:
    """Shingles used for near-match comparison: word n-grams for Latin text,
    character bigrams for CJK runs (see the module docstring)."""
    return set(_shingles_cached(text, n))


def _signature(sh: set[str] | frozenset[str], text: str, k: int) -> str:
    if not sh:
        return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:16]
    hashed = sorted(hashlib.sha1(s.encode()).hexdigest()[:8] for s in sh)
    return "".join(hashed[:k])


def content_hash(text: str, k: int = 16) -> str:
    """Compact signature: the k smallest shingle hashes joined.

    Two near-identical texts share most of their smallest shingle hashes, so
    the signatures collide on a shared prefix far more often than random text —
    enough to cheaply pre-filter dedup candidates.
    """
    return _signature(_shingles_cached(text, 3), text, k)


def legacy_content_hash(text: str, k: int = 16) -> str | None:
    """The fingerprint versions before CJK support stored, or None when that old
    fingerprint can't be trusted.

    Used only to recognise postings an older version already sent, so upgrading
    doesn't re-send them. It returns None for CJK text that also contains Latin
    words: the old code built those fingerprints from the Latin words alone, so
    unrelated posts collided (every Chinese post mentioning just "Golang" got
    the same one) — honouring them would keep hiding real jobs.
    """
    low = text.lower()
    toks = _LEGACY_TOKEN_RE.findall(low)
    if toks and has_cjk(text):
        return None
    return _signature(_word_shingles(toks, 3), text, k)


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles_cached(a, 3), _shingles_cached(b, 3)
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
