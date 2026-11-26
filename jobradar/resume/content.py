"""What goes on the résumé, in this fixed order:

name → job title (· focus) → professional summary → technical skills → work
experience → key projects → education → languages.

Two ways to fill it:

* :func:`from_profile` — straight from your profile (no AI), as you typed it.
* :func:`from_ai` — the AI's version tailored to one job post, **checked
  against your profile**. The AI must cite, for every skill and bullet, the
  exact profile text it came from; anything it can't back up is dropped and
  your own wording is used instead. Numbers (years, %, amounts) must appear in
  that source text. Dates, schools and employers always come from the profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..apply import language
from . import profile as prof


@dataclass
class Entry:
    title: str                                   # role / project name / "major · degree · school"
    org: str = ""                                # company, shown as "title — org" in the accent colour
    period: str = ""                             # right-aligned dates
    sub: str = ""                                # italic grey line: team / area · location
    meta: list[tuple[str, str]] = field(default_factory=list)   # grey line: ("技术栈：", "Go、gRPC")
    bullets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)              # small grey lines (education details)


@dataclass
class ResumeContent:
    lang: str
    name: str
    job_title: str
    focus: str = ""
    contact: list[str] = field(default_factory=list)
    intro: str = ""
    skills: list[tuple[str, str]] = field(default_factory=list)  # (label, text)
    experience: list[Entry] = field(default_factory=list)
    projects: list[Entry] = field(default_factory=list)
    education: list[Entry] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    tailored: bool = False

    @property
    def headline(self) -> str:
        return _join(self.job_title, self.focus)

    def plain_text(self) -> str:
        """Everything on the page, for font checks, tests and previews."""
        out = [self.name, self.headline, " · ".join(self.contact), self.intro]
        colon = "：" if self.lang == "zh" else ": "
        out += [f"{k}{colon}{v}" if k else v for k, v in self.skills]
        for e in self.experience + self.projects + self.education:
            out += [e.title, e.org, e.period, e.sub, *(f"{k}{v}" for k, v in e.meta), *e.bullets, *e.notes]
        out += self.languages
        return "\n".join(x for x in out if x)


def _period(start: str, end: str, lang: str) -> str:
    start, end = (start or "").strip(), (end or "").strip()
    if start and not end:
        end = language.t("cv_present", lang)
    return f"{start} — {end}" if start else end


def _join(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


def contact_line(p: dict[str, Any], lang: str) -> list[str]:
    out = []
    if p.get("gender") in ("male", "female"):
        out.append(language.t("cv_" + p["gender"], lang))
    if p.get("birth_date"):
        out.append(language.t("cv_born", lang, date=p["birth_date"]))
    out += [p[k] for k in ("location", "phone", "email") if p.get(k)]
    return out


def _language_line(lang_name: str, level: str, lang: str) -> str:
    if lang_name and level:
        return f"{lang_name}{'：' if lang == 'zh' else ': '}{level}"
    return lang_name or level


def _meta(stack: str, role: str, link: str, lang: str) -> list[tuple[str, str]]:
    out = []
    if stack:
        out.append((language.t("cv_stack", lang), stack))
    if role:
        out.append((language.t("cv_role", lang), role))
    if link:
        out.append(("", link))
    return out


def from_profile(p: dict[str, Any], lang: str) -> ResumeContent:
    p = prof.newest_first(prof.normalize(p))
    c = ResumeContent(lang=lang, name=p["name"], job_title=p["job_title"], focus=p["focus"],
                      contact=contact_line(p, lang), intro=p["summary"], skills=prof.skill_lines(p["skills"]))
    for e in p["experience"]:
        c.experience.append(Entry(e["role"] or e["company"], e["company"] if e["role"] else "",
                                  _period(e["start"], e["end"], lang), _join(e["area"], e["location"]),
                                  bullets=prof.lines(e["details"])))
    for e in p["projects"]:
        c.projects.append(Entry(e["name"], period=_period(e["start"], e["end"], lang),
                                meta=_meta(e["stack"], e["role"], e["link"], lang), bullets=prof.lines(e["details"])))
    for e in p["education"]:
        c.education.append(Entry(_join(e["major"], e["degree"], e["school"]), period=_period(e["start"], e["end"], lang),
                                 notes=prof.lines(e["details"])))
    c.languages = [_language_line(x["language"], x["level"], lang) for x in p["languages"]]
    return c


# ------------------------------------------------------------- AI checking
def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", (s or "").lower())


_UNITS = {"千": 1e3, "k": 1e3, "thousand": 1e3, "万": 1e4, "w": 1e4, "百万": 1e6, "m": 1e6, "million": 1e6,
          "亿": 1e8, "b": 1e9, "billion": 1e9}
_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(百万|千|万|亿|thousand|million|billion|[kKmMbBwW](?![a-zA-Z]))?")


def _number_pairs(s: str) -> list[tuple[float, float]]:
    """(as written, with its unit applied) for each number: "300万" → (300, 3e6)."""
    out = []
    for n, unit in _NUM.findall((s or "").replace(",", "")):
        v = float(n)
        out.append((v, v * _UNITS.get(unit.lower(), 1.0) if unit else v))
    return out


def _same_numbers(text: str, source: str) -> bool:
    """Every number in ``text`` is in ``source``, as written or converted
    ("300万" may become "3M" or "3 million"; "12" from nowhere may not)."""
    known = {round(x, 6) for pair in _number_pairs(source) for x in pair}
    return all(round(raw, 6) in known or round(scaled, 6) in known for raw, scaled in _number_pairs(text))


def _backed(text: str, source: str, allowed: str) -> bool:
    """True when ``source`` really is profile text (within ``allowed``) and the
    rewrite adds no numbers the source doesn't have."""
    src = _norm(source)
    if not text.strip() or len(src) < 2 or src not in _norm(allowed):
        return False
    return _same_numbers(text, source)


# Words that claim more responsibility or seniority. A rewrite may only use a
# group that its source already uses (in either language): "参与开发" may become
# "helped build", never "led"; "工程师" may become "Engineer", never "Senior Engineer".
_SCOPE_VERBS = {
    "lead": r"\bled\b|\bleading\b|\bheaded\b|\bspearhead|\bdrove\b|\bdirected\b|主导|牵头|带领|领导|率领|领衔",
    "own": r"\bowned\b|\bowner\b|\bownership\b|\bresponsible for\b|\bin charge of\b|负责",
    "design": r"\bdesign(?:ed|ing)?\b|\barchitect(?:ed|ing|ure)?\b|设计|架构",
    "manage": r"\bmanag(?:ed|ing)\b|\bsupervis(?:ed|ing)\b|\bmentor(?:ed|ing)?\b|管理|指导",
    "found": r"\bfounded\b|\bco-?founded\b|\binvented\b|\binitiated\b|\bpioneer|创立|创建|发起|首创",
}
_PARTICIPATION = (r"\bparticipat|\bassist|\bcontribut|\bhelped\b|\bsupported\b|\bjoined\b|\bas part of\b"
                  r"|参与|协助|配合|辅助")
_SENIORITY = {
    "senior": r"\bsenior\b|\bsr\b|高级|资深",
    "lead": r"\blead\b|\bleader\b|组长|负责人|带头人",
    "staff": r"\bstaff\b|\bprincipal\b|首席|专家",
    "manager": r"\bmanager\b|\bsupervisor\b|经理|主管",
    "head": r"\bhead\b|\bdirector\b|\bvp\b|\bvice president\b|总监|副总裁|总经理",
    "chief": r"\bchief\b|\bc[a-z]o\b",
    "architect": r"\barchitect\b|架构师",
}


def _groups(table: dict[str, str], text: str) -> set[str]:
    t = (text or "").lower()
    return {k for k, pat in table.items() if re.search(pat, t)}


def _no_inflation(text: str, source: str) -> bool:
    """The rewrite claims no more responsibility than its source: no new
    lead/own/design/manage/found verbs, and "participated / 协助" isn't dropped."""
    if not _groups(_SCOPE_VERBS, text) <= _groups(_SCOPE_VERBS, source):
        return False
    if re.search(_PARTICIPATION, (source or "").lower()) and not re.search(_PARTICIPATION, (text or "").lower()):
        return False
    return True


def _lang_of(text: str) -> str:
    return "zh" if re.search(r"[一-鿿]", text or "") else "en"


def _label(ai_value: Any, mine: str, lang: str, limit: int = 120) -> str:
    """A name or title from the profile, kept exactly as you wrote it when it is
    already in the résumé's language; otherwise the AI's translation, if it adds
    no numbers and no seniority ("工程师" → "Senior Engineer" is refused)."""
    if not mine:
        return ""
    if _lang_of(mine) == lang:
        return mine
    s = str(ai_value or "").strip()
    if not s or not _same_numbers(s, mine) or not _groups(_SENIORITY, s) <= _groups(_SENIORITY, mine):
        return mine
    return s[:limit]


def _bullets(ai_items: Any, details: str) -> list[str]:
    """AI bullets that cite this entry's own notes; your lines if none survive."""
    mine = prof.lines(details)
    out = []
    for b in ai_items if isinstance(ai_items, list) else []:
        if not isinstance(b, dict):
            continue
        text, source = str(b.get("text") or "").strip(), str(b.get("source") or "")
        if _backed(text, source, details) and _no_inflation(text, source):
            out.append(text[:500])
    return out or mine


def _by_index(items: Any, n: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for it in items if isinstance(items, list) else []:
        try:
            i = int(it.get("index"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < n and i not in out:
            out[i] = it
    return out


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def from_ai(obj: dict[str, Any] | None, p: dict[str, Any], lang: str) -> ResumeContent:
    """The AI's tailored résumé, with every claim checked against the profile.
    Falls back to :func:`from_profile` piece by piece."""
    p = prof.newest_first(prof.normalize(p))
    base = from_profile(p, lang)
    if not isinstance(obj, dict):
        return base
    corpus = prof.facts_text(p)
    c = ResumeContent(lang=lang, name=p["name"], job_title=_label(obj.get("job_title"), p["job_title"], lang, 80),
                      focus=_label(obj.get("focus"), p["focus"], lang, 80), contact=base.contact, tailored=True)

    intro = str(obj.get("intro") or "").strip()
    c.intro = (intro[:1500] if intro and _same_numbers(intro, corpus) and _no_inflation(intro, corpus)
               else base.intro)

    groups: list[tuple[str, str]] = []
    sep = "、" if lang == "zh" else ", "
    for g in obj.get("tech_stack") or []:
        if not isinstance(g, dict):
            continue
        items = []
        for it in g.get("items") or []:
            if isinstance(it, dict) and _backed(str(it.get("text") or ""), str(it.get("source") or ""), corpus):
                items.append(str(it["text"]).strip()[:80])
        if items:
            groups.append((str(g.get("category") or "").strip()[:40], sep.join(items)))
    c.skills = groups or base.skills

    ex = _by_index(obj.get("experience"), len(p["experience"]))
    for i, e in enumerate(p["experience"]):          # every job, in your order
        a, b = ex.get(i, {}), base.experience[i]
        role, company = _label(a.get("role"), e["role"], lang), _label(a.get("company"), e["company"], lang)
        c.experience.append(Entry(
            role or company, company if role else "", b.period,
            _join(_label(a.get("area"), e["area"], lang), _label(a.get("location"), e["location"], lang)),
            bullets=_bullets(a.get("bullets"), e["details"])))

    pr = _by_index(obj.get("projects"), len(p["projects"]))
    order = [i for i in (_int(x) for x in obj.get("project_order") or []) if i in range(len(p["projects"]))]
    order = list(dict.fromkeys(order + list(range(len(p["projects"])))))   # most relevant first, none lost
    for i in order:
        e, a = p["projects"][i], pr.get(i, {})
        c.projects.append(Entry(
            _label(a.get("name"), e["name"], lang), period=base.projects[i].period,
            meta=_meta(e["stack"], _label(a.get("role"), e["role"], lang), e["link"], lang),
            bullets=_bullets(a.get("bullets"), e["details"])))

    ed = _by_index(obj.get("education"), len(p["education"]))
    for i, e in enumerate(p["education"]):
        a = ed.get(i, {})
        c.education.append(Entry(
            _join(*(_label(a.get(k), e[k], lang) for k in ("major", "degree", "school"))),
            period=base.education[i].period, notes=_bullets(a.get("bullets"), e["details"])))

    la = _by_index(obj.get("languages"), len(p["languages"]))
    c.languages = [_language_line(_label(la.get(i, {}).get("language"), x["language"], lang, 60),
                                  _label(la.get(i, {}).get("level"), x["level"], lang, 60), lang)
                   for i, x in enumerate(p["languages"])]
    return c


def file_stem(p: dict[str, Any], lang: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", (p.get("name") or "").strip()).strip("_") or "Resume"
    return language.t("cv_file", lang, name=name)[:80]
