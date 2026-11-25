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

import base64
import hashlib
import re
import struct
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


# --------------------------------------------------------------- role guard
# Two postings from one company often share most of their text (the "about us"
# and benefits boilerplate) while being different jobs: a junior vs a senior
# opening, backend vs frontend. Text similarity can't tell those apart from a
# lightly reworded repost, but the title can. A posting's *role signature* is
# the seniority and function words in its title, normalised across English and
# Chinese. When both titles name a seniority (or both name a function) and the
# two sets don't overlap, the postings are different jobs — never duplicates.
_SENIORITY = {
    "intern": ["intern", "internship", "实习"],
    "junior": ["junior", "jr", "entry level", "entry-level", "graduate", "初级", "应届"],
    "mid": ["mid", "mid-level", "intermediate", "中级"],
    "senior": ["senior", "sr", "高级", "资深"],
    "staff": ["staff", "principal", "专家"],
    "lead": ["lead", "tech lead", "负责人", "组长"],
    "manager": ["manager", "head", "director", "vp", "主管", "经理", "总监"],
}
_FUNCTION = {
    "backend": ["backend", "back-end", "back end", "后端", "服务端"],
    "frontend": ["frontend", "front-end", "front end", "前端"],
    "fullstack": ["fullstack", "full-stack", "full stack", "全栈"],
    "mobile": ["mobile", "ios", "android", "flutter", "移动端", "客户端"],
    "data": ["data", "analytics", "数据"],
    "ml": ["ml", "machine learning", "ai", "llm", "算法", "机器学习"],
    "devops": ["devops", "sre", "platform", "infrastructure", "运维"],
    "qa": ["qa", "test", "testing", "sdet", "测试"],
    "security": ["security", "安全"],
    "design": ["designer", "design", "ux", "ui", "设计"],
    "product": ["product manager", "product owner", "产品经理"],
}


def _vocab_re(groups: dict[str, list[str]]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for canon, forms in groups.items():
        latin = [re.escape(f) for f in forms if f.isascii()]
        cjk = [re.escape(f) for f in forms if not f.isascii()]
        parts = []
        if latin:
            parts.append(r"(?<![a-z0-9])(?:" + "|".join(latin) + r")s?(?![a-z0-9])")
        if cjk:
            parts.append("|".join(cjk))
        out.append((canon, re.compile("|".join(parts))))
    return out


_SENIORITY_RE = _vocab_re(_SENIORITY)
_FUNCTION_RE = _vocab_re(_FUNCTION)


def role_signature(title: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """(seniority words, function words) found in a posting title."""
    t = _normalise(title or "").replace(".", " ")
    return (frozenset(c for c, rx in _SENIORITY_RE if rx.search(t)),
            frozenset(c for c, rx in _FUNCTION_RE if rx.search(t)))


def roles_conflict(a: tuple[frozenset[str], frozenset[str]],
                   b: tuple[frozenset[str], frozenset[str]]) -> bool:
    """True when two role signatures clearly describe different jobs."""
    return any(x and y and not (x & y) for x, y in zip(a, b))


def is_duplicate(
    text_a: str,
    text_b: str,
    url_a: str | None = None,
    url_b: str | None = None,
    *,
    title_a: str | None = None,
    title_b: str | None = None,
    threshold: float = 0.6,
) -> bool:
    """Decide whether two postings are the same job.

    Titles naming clearly different roles (junior vs senior, backend vs
    frontend) are never duplicates. Otherwise a matching normalised application
    URL is decisive, or the texts must be sufficiently similar by shingle Jaccard.
    """
    if roles_conflict(role_signature(title_a), role_signature(title_b)):
        return False
    if url_a and url_b and url_a == url_b:
        return True
    return jaccard(text_a, text_b) >= threshold


# ------------------------------------------------- cross-run repost memory
# Within one run, postings are compared directly (exact Jaccard above). Across
# runs we can't keep every past posting's text, so each sent posting leaves a
# MinHash signature: 64 numbers whose pairwise agreement rate estimates the
# Jaccard similarity of the underlying shingle sets (±0.06 at 0.6). Signatures
# are split into bands for a locality-sensitive-hashing index, so checking a
# new posting against thousands of remembered ones is a few dictionary lookups.
MINHASH_K = 64
_BANDS, _ROWS = 16, 4              # 16 × 4 = 64; finds pairs at J≥0.7 ~99% of the time
_MERSENNE = (1 << 61) - 1
# Fixed coefficients: signatures are persisted, so these must never change.
_rng = __import__("random").Random(0x6A6F6272)
_PERMS = [(_rng.randrange(1, _MERSENNE), _rng.randrange(0, _MERSENNE)) for _ in range(MINHASH_K)]
del _rng
# Very short posts ("Hiring React dev, DM me") are too generic to near-match
# safely across weeks; they are only ever matched exactly.
MIN_SHINGLES_FOR_REPOST = 8


def minhash(text: str) -> str | None:
    """Compact MinHash signature of a posting's text (base64), or None if the
    text is too short to near-match safely."""
    sh = _shingles_cached(text, 3)
    if len(sh) < MIN_SHINGLES_FOR_REPOST:
        return None
    xs = [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") for s in sh]
    vals = [min((a * x + b) % _MERSENNE for x in xs) & 0xFFFFFFFF for a, b in _PERMS]
    return base64.b64encode(struct.pack(f"<{MINHASH_K}I", *vals)).decode("ascii")


def _unpack(sig: str) -> tuple[int, ...]:
    return struct.unpack(f"<{MINHASH_K}I", base64.b64decode(sig))


def estimate_similarity(sig_a: str, sig_b: str) -> float:
    a, b = _unpack(sig_a), _unpack(sig_b)
    return sum(x == y for x, y in zip(a, b)) / MINHASH_K


def encode_roles(roles: tuple[frozenset[str], frozenset[str]]) -> str:
    return " ".join(sorted(roles[0])) + "|" + " ".join(sorted(roles[1]))


def decode_roles(s: str) -> tuple[frozenset[str], frozenset[str]]:
    sen, _, fun = (s or "|").partition("|")
    return frozenset(sen.split()), frozenset(fun.split())


class RepostIndex:
    """LSH index over remembered signatures: ``{fingerprint: (sig, roles)}``."""

    def __init__(self, entries: dict[str, tuple[str, str]] | None = None):
        self._entries: dict[str, tuple[tuple[int, ...], tuple[frozenset[str], frozenset[str]]]] = {}
        self._bands: dict[tuple[int, tuple[int, ...]], set[str]] = {}
        for fp, (sig, roles) in (entries or {}).items():
            self.add(fp, sig, roles)

    def add(self, fingerprint: str, sig: str, roles: str) -> None:
        try:
            vals = _unpack(sig)
        except (ValueError, struct.error):
            return  # corrupt entry in the state file: ignore it
        self._entries[fingerprint] = (vals, decode_roles(roles))
        for i in range(_BANDS):
            self._bands.setdefault((i, vals[i * _ROWS:(i + 1) * _ROWS]), set()).add(fingerprint)

    def find(self, sig: str, roles: tuple[frozenset[str], frozenset[str]], *,
             threshold: float = 0.6) -> str | None:
        """Fingerprint of the most similar remembered posting at or above
        ``threshold`` (estimated) whose role doesn't conflict, or None."""
        vals = _unpack(sig)
        candidates: set[str] = set()
        for i in range(_BANDS):
            candidates |= self._bands.get((i, vals[i * _ROWS:(i + 1) * _ROWS]), set())
        best, best_sim = None, threshold
        for fp in candidates:
            other, other_roles = self._entries[fp]
            sim = sum(x == y for x, y in zip(vals, other)) / MINHASH_K
            if sim >= best_sim and not roles_conflict(roles, other_roles):
                best, best_sim = fp, sim
        return best

    def __len__(self) -> int:
        return len(self._entries)
