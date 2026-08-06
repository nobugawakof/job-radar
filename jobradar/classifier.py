"""Job detection.

FR-7/FR-8: classify each collected post as *job posting* or *not*, and favour
recall over precision — a missed posting is unrecoverable, a false positive is
just noise the user dismisses. The classifier is therefore intentionally
generous: a post is treated as a posting if it shows *any* hiring signal, and
signals are expressed as data (NFR-14) so they can be tuned without code
changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Strong signals — presence of one is enough on its own.
HIRING_PHRASES = [
    "hiring", "we're hiring", "were hiring", "now hiring", "looking for",
    "seeking", "join our team", "join the team", "job opening", "job opportunity",
    "open position", "open role", "vacancy", "vacancies", "apply now",
    "apply here", "send your resume", "send your cv", "who is hiring",
    "who's hiring", "recruiting", "we are looking", "position available",
    "full-time", "full time", "part-time", "part time", "contract role",
    "freelance", "job posting", "career opportunity", "employment opportunity",
    "dm me your", "reach out if", "role available", "we need a", "we need an",
]

# Role-title signals — common tech titles that, combined with weak context,
# indicate a posting.
ROLE_TERMS = [
    "engineer", "developer", "designer", "manager", "architect", "analyst",
    "scientist", "devops", "sre", "researcher", "founder", "cto", "lead",
    "programmer", "administrator", "consultant", "specialist", "intern",
]

# Compensation / application signals.
CONTEXT_TERMS = [
    "salary", "compensation", "comp", "equity", "tokens", "remote", "onsite",
    "on-site", "hybrid", "relocation", "benefits", "apply", "resume", "cv",
    "email us", "send us", "€", "$", "£", "usd", "eur", "gbp", "per year",
    "per hour", "annual", "stipend",
]

_STRONG_RE = re.compile("|".join(re.escape(p) for p in HIRING_PHRASES), re.I)
_ROLE_RE = re.compile(r"\b(" + "|".join(ROLE_TERMS) + r")s?\b", re.I)
_CONTEXT_RE = re.compile("|".join(re.escape(t) for t in CONTEXT_TERMS), re.I)

# Chinese signals. Chinese has no word boundaries, so these are matched as plain
# substrings rather than with \b. Lets the pipeline detect Chinese job posts
# (e.g. from v2ex, ruby-china) instead of dropping them as "not a job".
CHINESE_HIRING = [
    "招聘", "诚聘", "热招", "急聘", "招募", "招人", "内推", "在招", "招贤",
    "岗位", "职位", "求贤", "职位描述", "岗位职责", "任职要求", "岗位要求",
    "工作职责", "职责", "我们在招", "正在招",
]
CHINESE_ROLE = [
    "工程师", "开发", "程序员", "架构师", "设计师", "产品经理", "运维",
    "测试", "前端", "后端", "全栈", "算法", "数据", "运营",
]
CHINESE_CONTEXT = [
    "远程", "远端", "在家办公", "居家办公", "薪资", "薪酬", "月薪", "年薪",
    "福利", "全职", "兼职", "实习", "股权", "期权", "简历", "投递", "面试",
]


@dataclass(frozen=True)
class Classification:
    is_posting: bool
    score: float
    signals: list[str]


def classify(text: str, *, source_prior: float = 0.0) -> Classification:
    """Classify a post.

    ``source_prior`` lets a source that only ever emits jobs (e.g. an HN
    "Who Is Hiring" thread or a dedicated jobs channel) raise the baseline so
    borderline posts are kept — recall-first (FR-8).
    """
    if not text or not text.strip():
        return Classification(False, 0.0, [])

    signals: list[str] = []
    score = source_prior

    if _STRONG_RE.search(text) or any(t in text for t in CHINESE_HIRING):
        score += 1.0
        signals.append("hiring_phrase")

    has_role = bool(_ROLE_RE.search(text)) or any(t in text for t in CHINESE_ROLE)
    has_context = bool(_CONTEXT_RE.search(text)) or any(t in text for t in CHINESE_CONTEXT)
    if has_role:
        score += 0.5
        signals.append("role_term")
    if has_context:
        score += 0.4
        signals.append("context_term")

    # Role + context together is a posting even without an explicit "hiring".
    if has_role and has_context:
        score += 0.3
        signals.append("role+context")

    # Recall-first threshold: anything at or above 0.7 is kept. With a source
    # prior of 1.0 (a jobs-only feed), every post clears the bar.
    is_posting = score >= 0.7
    return Classification(is_posting, round(score, 3), signals)
