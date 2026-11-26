"""What goes on the résumé, in this fixed order:

name → job title → personal introduction → tech stack → work experience →
notable projects → education → languages.

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
    title: str                 # company / project / school
    subtitle: str = ""         # role · location / degree · major
    period: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class ResumeContent:
    lang: str
    name: str
    job_title: str
    contact: list[str] = field(default_factory=list)
    intro: str = ""
    skills: list[tuple[str, list[str]]] = field(default_factory=list)
    experience: list[Entry] = field(default_factory=list)
    projects: list[Entry] = field(default_factory=list)
    education: list[Entry] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    tailored: bool = False

    def plain_text(self) -> str:
        """Everything on the page, for tests and previews."""
        out = [self.name, self.job_title, " · ".join(self.contact), self.intro]
        out += [f"{k}: {', '.join(v)}" if k else ", ".join(v) for k, v in self.skills]
        for e in self.experience + self.projects + self.education:
            out += [e.title, e.subtitle, e.period, *e.bullets]
        out += self.languages
        return "\n".join(x for x in out if x)


def _period(start: str, end: str, lang: str) -> str:
    start, end = (start or "").strip(), (end or "").strip()
    if start and not end:
        end = language.t("cv_present", lang)
    return f"{start} – {end}" if start else end


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


def from_profile(p: dict[str, Any], lang: str) -> ResumeContent:
    p = prof.normalize(p)
    c = ResumeContent(lang=lang, name=p["name"], job_title=p["job_title"], contact=contact_line(p, lang),
                      intro=p["summary"], skills=prof.skill_groups(p["skills"]))
    for e in p["experience"]:
        c.experience.append(Entry(e["company"] or e["role"], _join(e["role"] if e["company"] else "", e["location"]),
                                  _period(e["start"], e["end"], lang), prof.lines(e["details"])))
    for e in p["projects"]:
        c.projects.append(Entry(e["name"], _join(e["role"], e["link"]), _period(e["start"], e["end"], lang),
                                prof.lines(e["details"])))
    for e in p["education"]:
        c.education.append(Entry(e["school"] or e["degree"], _join(e["degree"] if e["school"] else "", e["major"]),
                                 _period(e["start"], e["end"], lang), prof.lines(e["details"])))
    c.languages = [_join(x["language"], x["level"]) for x in p["languages"]]
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


def _bullets(ai_items: Any, details: str) -> list[str]:
    """AI bullets that cite this entry's own notes; your lines if none survive."""
    mine = prof.lines(details)
    out = []
    for b in ai_items if isinstance(ai_items, list) else []:
        if not isinstance(b, dict):
            continue
        text, source = str(b.get("text") or "").strip(), str(b.get("source") or "")
        if _backed(text, source, details):
            out.append(text[:400])
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


def _short(v: Any, fallback: str, limit: int = 120) -> str:
    s = str(v or "").strip()
    return s[:limit] if s else fallback


def from_ai(obj: dict[str, Any] | None, p: dict[str, Any], lang: str) -> ResumeContent:
    """The AI's tailored résumé, with every claim checked against the profile.
    Falls back to :func:`from_profile` piece by piece."""
    p = prof.normalize(p)
    base = from_profile(p, lang)
    if not isinstance(obj, dict):
        return base
    corpus = prof.facts_text(p)
    c = ResumeContent(lang=lang, name=p["name"], job_title=_short(obj.get("job_title"), p["job_title"], 80),
                      contact=base.contact, tailored=True)

    intro = str(obj.get("intro") or "").strip()
    c.intro = intro[:900] if intro and _same_numbers(intro, corpus) else base.intro

    groups: list[tuple[str, list[str]]] = []
    for g in obj.get("tech_stack") or []:
        if not isinstance(g, dict):
            continue
        items = []
        for it in g.get("items") or []:
            if isinstance(it, dict) and _backed(str(it.get("text") or ""), str(it.get("source") or ""), corpus):
                items.append(str(it["text"]).strip()[:60])
        if items:
            groups.append((str(g.get("category") or "").strip()[:40], items))
    c.skills = groups or base.skills

    ex = _by_index(obj.get("experience"), len(p["experience"]))
    for i, e in enumerate(p["experience"]):          # every job, in your order
        a = ex.get(i, {})
        c.experience.append(Entry(
            _short(a.get("company"), base.experience[i].title),
            _join(_short(a.get("role"), e["role"]), _short(a.get("location"), e["location"])),
            base.experience[i].period, _bullets(a.get("bullets"), e["details"])))

    pr = _by_index(obj.get("projects"), len(p["projects"]))
    order = [i for i in (_int(x) for x in obj.get("project_order") or []) if i in range(len(p["projects"]))]
    order = list(dict.fromkeys(order + list(range(len(p["projects"])))))   # most relevant first, none lost
    for i in order:
        e, a = p["projects"][i], pr.get(i, {})
        c.projects.append(Entry(_short(a.get("name"), e["name"]), _join(_short(a.get("role"), e["role"]), e["link"]),
                                base.projects[i].period, _bullets(a.get("bullets"), e["details"])))

    ed = _by_index(obj.get("education"), len(p["education"]))
    for i, e in enumerate(p["education"]):
        a = ed.get(i, {})
        c.education.append(Entry(
            _short(a.get("school"), base.education[i].title),
            _join(_short(a.get("degree"), e["degree"]), _short(a.get("major"), e["major"])),
            base.education[i].period, _bullets(a.get("bullets"), e["details"])))

    la = _by_index(obj.get("languages"), len(p["languages"]))
    c.languages = [_short(la.get(i, {}).get("text"), base.languages[i]) for i in range(len(p["languages"]))]
    return c


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def file_stem(p: dict[str, Any], lang: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", (p.get("name") or "").strip()).strip("_") or "Resume"
    return language.t("cv_file", lang, name=name)[:80]
