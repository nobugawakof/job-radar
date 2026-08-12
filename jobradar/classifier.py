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
# substrings. Only genuine "a company is hiring" verbs go here — words like
# 岗位/职位 were moved to CONTEXT because they show up just as often in market
# discussion and job-seeker posts.
CHINESE_HIRING = [
    "招聘", "诚聘", "热招", "急聘", "招募", "招人", "在招", "招贤", "招 ",
    "正在招", "我们在招", "高薪招", "求贤", "内推",
]
CHINESE_ROLE = [
    "工程师", "开发", "程序员", "架构师", "设计师", "产品经理", "运维",
    "测试", "前端", "后端", "全栈", "算法", "数据", "运营",
]
CHINESE_CONTEXT = [
    "远程", "远端", "在家办公", "居家办公", "薪资", "薪酬", "月薪", "年薪",
    "福利", "全职", "兼职", "实习", "股权", "期权", "简历", "投递", "面试",
    "岗位", "职位", "岗位职责", "任职要求", "岗位要求", "工作职责", "职责",
]

# Negative signals: job-SEEKER, advice, layoff-venting, and discussion posts —
# NOT job openings. "Hard" terms are unambiguous seeking/venting and always
# reject the post (even if it also contains a hiring verb — e.g. "求内推" holds
# 内推 but is clearly someone seeking). "Soft" terms only reject when there's no
# genuine hiring verb, so a real opening that happens to mention one is kept.
HARD_SEEKER_ZH = [
    "求助", "求职", "求带", "求内推", "求推荐", "求经验", "求指导", "找工作",
    "被裁", "裁员", "职业方向", "职业规划", "内推我", "内推求", "跪求",
]
HARD_SEEKER_EN = [
    "who wants to be hired", "looking for a job", "looking for work",
    "got laid off", "laid off", "resume review",
    # Someone advertising THEMSELVES for work (freelancer / job seeker), which
    # is what "[for hire]" and first-person availability phrases signal.
    "for hire", "hire me", "available for hire", "available for freelance",
    "open to work", "open to opportunities", "open to new opportunit",
    "currently looking for", "currently seeking", "actively looking",
    "actively seeking", "i'm looking for", "im looking for", "i am looking for",
    "i'm seeking", "im seeking", "i am seeking", "i'm available", "im available",
    "i'm a freelance", "im a freelance", "i am a freelance", "freelancer available",
    "looking for opportunities", "looking for new opportunit", "looking for a role",
    "looking for a position", "seeking opportunities", "seeking a role",
    "seeking a position", "my portfolio", "immediate joiner",
]
SOFT_SEEKER_ZH = [
    "毕业", "应届", "迷茫", "请教", "咨询一下", "吐槽", "转行", "帮看看",
    "分享一下", "讨论一下", "有没有推荐", "怎么办", "感想", "心得", "求偏门",
]
SOFT_SEEKER_EN = [
    "seeking work", "job seeking", "career advice", "advice needed",
    "any tips", "rant",
]


def _has_cjk(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in text)


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

    # Normalise curly apostrophes so "I’m" matches the same as "I'm".
    low = text.lower().replace("’", "'")
    signals: list[str] = []
    score = source_prior

    strong = bool(_STRONG_RE.search(text)) or any(t in text for t in CHINESE_HIRING)

    # Hard seeker/discussion markers always reject (even with a hiring verb).
    if any(n in text for n in HARD_SEEKER_ZH) or any(n in low for n in HARD_SEEKER_EN):
        return Classification(False, 0.0, ["seeker_or_discussion"])
    # Soft markers reject only when there's no genuine hiring verb.
    soft = any(n in text for n in SOFT_SEEKER_ZH) or any(n in low for n in SOFT_SEEKER_EN)
    if soft and not strong:
        return Classification(False, 0.0, ["seeker_or_discussion"])

    if strong:
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

    # Recall-first threshold: anything at or above 0.7 is kept.
    is_posting = score >= 0.7

    # Chinese content is noisier (forums mix hiring, seeking, and chatter), so a
    # Chinese post needs a real hiring verb — role+context alone isn't enough.
    # A dedicated jobs feed (source_prior >= 1.0) is exempt.
    if is_posting and _has_cjk(text) and not strong and source_prior < 1.0:
        is_posting = False
        signals.append("cjk_no_hiring_verb")

    return Classification(is_posting, round(score, 3), signals)
