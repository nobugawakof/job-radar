"""Salary parsing and a rough annual-USD estimate for the salary filter.

Job posts write pay in many ways: "$120k-$150k", "HK$35,000/month",
"€550/day", "月薪 20-30K·14薪", "年薪 50-80万", "2万/月". This module finds the
amount that is really the salary and works out currency and pay period.

Rules that matter for not dropping good jobs:

* A number only counts as pay when something says so: a currency, a salary
  label ("Salary:", "Compensation", 月薪, 待遇…), a pay period ("/hour",
  "/月") or a months-per-year marker ("14薪"). So "401k", "5+ years",
  "founded in 2015" and "90%" are ignored.
* Money that isn't pay is skipped: amounts in millions/billions, and figures
  next to "raised", "funding", "Series A", "valuation", "ARR", 融资, 估值….
* The pay period is taken only from right next to the amount (or from a label
  like 月薪/年薪), never from elsewhere in the post ("4 weeks per year of
  PTO" is not the salary's period). Chinese K-figures are monthly by
  convention. When the period genuinely can't be told, it stays unknown
  (SRS FR-11) and the salary filter lets the job through rather than guess.

Exchange rates are approximate and static: the filter answers "is this
roughly above my floor?", not accounting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Salary:
    raw: str | None = None        # as written, e.g. "月薪 20-30K·14薪"
    min: float | None = None      # in `currency`, per `period`
    max: float | None = None
    currency: str | None = None   # ISO 4217, e.g. "HKD"
    period: str | None = None     # hour / day / week / month / year, or None = unknown
    months: int | None = None     # paid months per year (e.g. 14 for "14薪"); None = 12


# Approximate USD value of one unit (late-2026 ballpark).
USD_RATES: dict[str, float] = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CHF": 1.13, "CAD": 0.73, "AUD": 0.66, "NZD": 0.60,
    "SGD": 0.74, "HKD": 0.128, "CNY": 0.14, "TWD": 0.031, "JPY": 0.0067, "KRW": 0.00073,
    "INR": 0.012, "MYR": 0.21, "PHP": 0.017, "IDR": 0.000063, "THB": 0.028, "VND": 0.00004,
    "AED": 0.27, "ILS": 0.27, "SEK": 0.095, "NOK": 0.093, "DKK": 0.145, "PLN": 0.25,
    "CZK": 0.043, "HUF": 0.0028, "BRL": 0.18, "MXN": 0.055, "ZAR": 0.054, "TRY": 0.03,
}

# Symbols/prefixes before the amount (longest first when matching).
_PREFIX = {
    "US$": "USD", "S$": "SGD", "SG$": "SGD", "HK$": "HKD", "A$": "AUD", "AU$": "AUD", "C$": "CAD",
    "CA$": "CAD", "NZ$": "NZD", "NT$": "TWD", "R$": "BRL", "MX$": "MXN", "$": "USD", "€": "EUR",
    "£": "GBP", "₹": "INR", "₩": "KRW", "₱": "PHP", "฿": "THB", "RM": "MYR", "Rp": "IDR",
    "¥": "¥", "￥": "¥",  # yuan or yen: decided from the text
}
_CODES = sorted(USD_RATES) + ["RMB"]
_SUFFIX_WORDS = {
    "元": "CNY", "块": "CNY", "人民币": "CNY", "美元": "USD", "美金": "USD", "港币": "HKD", "港元": "HKD",
    "新币": "SGD", "新加坡元": "SGD", "日元": "JPY", "欧元": "EUR", "英镑": "GBP", "马币": "MYR",
    "令吉": "MYR", "新台币": "TWD", "台币": "TWD", "澳元": "AUD", "加元": "CAD",
}

_PERIODS = [  # (pattern, period) — checked right after the amount
    (r"/\s*(?:hour|hr|h)\b|\bper\s+hour\b|\ban\s+hour\b|\bhourly\b|/\s*(?:小时|时)|每小时", "hour"),
    (r"/\s*day\b|\bper\s+day\b|\ba\s+day\b|\bdaily\b|\bday\s+rate\b|/\s*(?:天|日)|每天|每日", "day"),
    (r"/\s*(?:week|wk)\b|\bper\s+week\b|\ba\s+week\b|\bweekly\b|/\s*周|每周", "week"),
    (r"/\s*(?:month|mo|mth)\b|\bper\s+month\b|\ba\s+month\b|\bmonthly\b|\bp\.?m\.?(?![a-z])|/\s*月|每月", "month"),
    (r"/\s*(?:year|yr|annum)\b|\bper\s+(?:year|annum)\b|\ba\s+year\b|\bannually\b|\bannual\b|"
     r"\bp\.?a\.?(?![a-z])|\byearly\b|/\s*年|每年", "year"),
]
_PERIOD_RES = [(re.compile(p, re.I), name) for p, name in _PERIODS]
_LABEL_PERIOD = {"月薪": "month", "年薪": "year", "日薪": "day", "时薪": "hour", "周薪": "week"}
_LABEL_RE = re.compile(
    r"(?:\b(?:salary|salaries|compensation|comp|pay|paying|base|OTE|TC|rate|budget|stipend|wage)\b|"
    r"薪资|薪酬|月薪|年薪|日薪|时薪|周薪|待遇|工资|薪水|底薪|税前|薪)", re.I)
_NOT_PAY_RE = re.compile(
    r"\b(?:raised?|raising|funding|funded|valuation|valued|revenue|ARR|MRR|GMV|series\s+[a-e]|seed|"
    r"investors?|backed|grant|prize|bounty)\b|融资|估值|营收|收入规模|市值", re.I)

_NUM = r"\d{1,3}(?:[,.\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT = r"(?:[kK](?![a-zA-Z])|千|万|[wW](?![a-zA-Z])|(?:m|M|mn|MM|million|bn|B|billion)(?![a-zA-Z]))"
_SEP = r"\s*(?:-|–|—|~|～|to|至|到)\s*"
_PREFIX_ALT = "|".join(re.escape(k) for k in sorted(_PREFIX, key=len, reverse=True))
_CODE_ALT = "|".join(_CODES)
_SUFFIX_ALT = "|".join(re.escape(k) for k in sorted(_SUFFIX_WORDS, key=len, reverse=True))
_CANDIDATE = re.compile(
    rf"(?:(?P<p1>{_PREFIX_ALT})|(?P<c1>\b(?:{_CODE_ALT})\b))?\s?"
    rf"(?P<a>{_NUM})\s?(?P<ma>{_MULT})?"
    rf"(?:{_SEP}(?:(?P<p2>{_PREFIX_ALT})|(?P<c2>\b(?:{_CODE_ALT})\b))?\s?(?P<b>{_NUM})\s?(?P<mb>{_MULT})?)?"
    rf"(?:\s?(?P<c3>\b(?:{_CODE_ALT})\b|{_SUFFIX_ALT}))?")
_MONTHS_RE = re.compile(r"^[\s·•,，x×*]*(\d{2})\s*(?:薪|months?\b)", re.I)
_MONTHS_ZH_RE = re.compile(r"^[^\d\n]{0,8}?(\d{2})\s*薪")          # "…/月，16薪"
_PERIOD_BEFORE_RE = re.compile(
    r"\b(day|daily|hour|hourly|week|weekly|month|monthly|annual|yearly|year)\s+"
    r"(?:base\s+)?(?:rate|salary|pay|wage|compensation|comp|budget)\s*[:：]?\s*$", re.I)
_BEFORE_WORD_PERIOD = {"day": "day", "daily": "day", "hour": "hour", "hourly": "hour", "week": "week",
                       "weekly": "week", "month": "month", "monthly": "month", "annual": "year",
                       "yearly": "year", "year": "year"}
_CJK_RE = re.compile(r"[一-鿿]")
_KANA_RE = re.compile(r"[぀-ヿ]")


def _number(digits: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:[,.\s]\d{3})+(?:\.\d+)?", digits):
        head, _, frac = digits.rpartition(".") if re.search(r"\.\d{1,2}$", digits) else (digits, "", "")
        return float(re.sub(r"[,.\s]", "", head) + ("." + frac if frac else ""))
    return float(digits)


def _multiplier(m: str | None) -> float | None:
    """1000 for k/千, 10000 for 万/w; None for millions/billions (not pay)."""
    if not m:
        return 1.0
    if m in ("k", "K", "千"):
        return 1000.0
    if m in ("万", "w", "W"):
        return 10000.0
    return None


@dataclass
class _Candidate:
    start: int
    end: int
    lo: float
    hi: float
    currency: str | None
    period: str | None
    months: int | None
    label: str | None
    score: int


def _candidates(text: str):
    for m in _CANDIDATE.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        after = text[m.end():m.end() + 30]
        if after[:1] == "%" or re.match(r"\s*(?:years?|yrs?|\+\s*years?|employees|people|users|customers)\b", after, re.I):
            continue
        ma, mb = _multiplier(m.group("ma")), _multiplier(m.group("mb"))
        if ma is None or mb is None or _NOT_PAY_RE.search(before):
            continue  # millions/billions or funding: not pay
        lo = _number(m.group("a"))
        hi = _number(m.group("b")) if m.group("b") else None
        # "20-30K", "50-80万": a multiplier written once applies to both ends.
        if hi is not None and m.group("mb") and not m.group("ma") and lo < 1000:
            ma = mb
        if hi is not None and m.group("ma") and not m.group("mb") and hi < 1000:
            mb = ma
        lo *= ma
        hi = hi * mb if hi is not None else lo
        if hi < lo:
            lo, hi = hi, lo

        sym = m.group("p1") or m.group("p2")
        code = m.group("c1") or m.group("c2") or m.group("c3")
        currency = _PREFIX.get(sym) if sym else (_SUFFIX_WORDS.get(code) or ("CNY" if code == "RMB" else code))

        period = None
        for rx, name in _PERIOD_RES:
            if rx.match(after.lstrip()) or rx.match(after):
                period = name
                break
        mm = _MONTHS_RE.match(after) or _MONTHS_ZH_RE.match(after)
        months = int(mm.group(1)) if mm and 12 <= int(mm.group(1)) <= 18 else None
        lab = None
        for lm in _LABEL_RE.finditer(before[-30:]):
            lab = lm.group(0)
        if period is None and lab in _LABEL_PERIOD:
            period = _LABEL_PERIOD[lab]
        pb = _PERIOD_BEFORE_RE.search(before)
        if period is None and pb:
            period = _BEFORE_WORD_PERIOD[pb.group(1).lower()]  # "Day rate £600"

        # Only count it as pay if something says so.
        if not (currency or lab or period or months):
            continue
        if not currency and not (m.group("ma") or m.group("mb")) and lo < 1000 and period is None:
            continue  # a small bare number near a label ("4 weeks") isn't a salary
        score = (3 if lab else 0) + (2 if currency else 0) + (2 if period else 0) + (1 if m.group("b") else 0) \
            + (1 if m.group("ma") or m.group("mb") else 0) + (2 if months else 0)
        end = m.end()
        if mm:
            end = m.end() + mm.end()
        yield _Candidate(m.start(), end, lo, hi, currency, period, months, lab, score), mm


def parse_salary(text: str) -> Salary:
    """The post's salary, with currency and period where they can be told."""
    if not text:
        return Salary()
    best = None
    for cand, _ in _candidates(text):
        if best is None or cand.score > best.score:
            best = cand
    if best is None:
        return Salary()
    c = best
    chinese = bool(_CJK_RE.search(text)) and not _KANA_RE.search(text)
    currency = c.currency
    if currency == "¥":
        # Yuan or yen: the job's place decides (Tokyo/東京 → yen) before the
        # script does, since Japanese also uses Chinese characters.
        from .places import find

        where = find(text)
        currency = "JPY" if ("JP" in where and "CN" not in where) else ("CNY" if chinese else "JPY")
    if currency is None and (chinese or (c.label and _CJK_RE.search(c.label)) or c.months):
        currency = "CNY"

    period = c.period
    if period is None and c.months:
        period = "month"
    if period is None and currency == "CNY":
        # Chinese salaries in K are monthly by convention; 万 figures of
        # 10万+ are annual ("年薪 50-80万"), smaller ones monthly ("2万").
        period = "year" if c.hi >= 100_000 else "month"
    if period is None:
        usd = c.hi * USD_RATES.get(currency or "USD", 1.0)
        if usd >= 15_000:
            period = "year"  # "$120k", "€45k": clearly annual
        # otherwise ambiguous ("Salary: $8,000"): leave unknown

    raw_start = c.start
    if c.label:
        ls = text.rfind(c.label, max(0, c.start - 30), c.start)
        if ls >= 0:
            raw_start = ls
            pb = _PERIOD_BEFORE_RE.search(text[max(0, c.start - 40):c.start])
            if pb:  # include "Day" in "Day rate £600"
                raw_start = min(raw_start, max(0, c.start - 40) + pb.start())
    raw_end = c.end
    tail = text[c.end:c.end + 25]
    for rx, _ in _PERIOD_RES:
        pm = rx.match(tail.lstrip()) or rx.match(tail)
        if pm:
            raw_end = c.end + (len(tail) - len(tail.lstrip()) if rx.match(tail.lstrip()) else 0) + pm.end()
            break
    return Salary(raw=text[raw_start:raw_end].strip(" ,;，；"), min=c.lo, max=c.hi,
                  currency=currency, period=period, months=c.months)


_PERIOD_FACTOR = {"hour": 2080.0, "day": 260.0, "week": 52.0, "year": 1.0}


def annual_usd(salary: Salary) -> float | None:
    """Rough annual USD of the top of the range, or None when the amount or
    its pay period can't be told (then the salary filter doesn't judge it)."""
    amount = salary.max or salary.min
    if amount is None or salary.period is None:
        return None
    rate = USD_RATES.get((salary.currency or "USD").upper(), 1.0)
    factor = float(salary.months or 12) if salary.period == "month" else _PERIOD_FACTOR.get(salary.period)
    return amount * factor * rate if factor else None
