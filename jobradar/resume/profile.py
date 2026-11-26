"""Your profile: the facts a résumé is built from.

Required: name, job title, and at least one education entry. Everything else is
optional, and a section with nothing entered is simply left out of the résumé.
The AI never adds facts that aren't here (see :mod:`.content`).
"""

from __future__ import annotations

import re
from typing import Any

BASIC = ("name", "gender", "birth_date", "location", "phone", "email", "job_title", "focus")
REQUIRED = ("name", "job_title")
GENDERS = ("", "male", "female")

# list sections -> their fields
LISTS: dict[str, tuple[str, ...]] = {
    "education": ("school", "degree", "major", "start", "end", "details"),
    "experience": ("company", "role", "area", "location", "start", "end", "details"),
    "projects": ("name", "role", "stack", "link", "start", "end", "details"),
    "languages": ("language", "level"),
}
_LIMITS = {"details": 3000, "summary": 3000, "skills": 3000, "stack": 500, "link": 300, "start": 20, "end": 20}
_MAX_ITEMS = 30
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProfileError(ValueError):
    pass


def empty() -> dict[str, Any]:
    return {**{k: "" for k in BASIC}, "summary": "", "skills": "", **{k: [] for k in LISTS}}


def _clean(value: Any, limit: int = 200) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()[:limit]


def normalize(data: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce whatever was posted or stored into the profile shape (no checks)."""
    data = data or {}
    out = empty()
    for k in BASIC:
        out[k] = _clean(data.get(k), 200)
    if out["gender"].lower() in ("male", "m", "男"):
        out["gender"] = "male"
    elif out["gender"].lower() in ("female", "f", "女"):
        out["gender"] = "female"
    elif out["gender"]:
        out["gender"] = ""
    for k in ("summary", "skills"):
        out[k] = _clean(data.get(k), _LIMITS[k])
    for section, fields in LISTS.items():
        items = data.get(section) or []
        if not isinstance(items, list):
            continue
        for item in items[:_MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            row = {f: _clean(item.get(f), _LIMITS.get(f, 200)) for f in fields}
            if any(row.values()):
                out[section].append(row)
    return out


def validate(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize, then check the required fields. Raises ProfileError."""
    p = normalize(data)
    missing = [label for key, label in (("name", "Name"), ("job_title", "Job title")) if not p[key]]
    if not any(e["school"] or e["degree"] for e in p["education"]):
        missing.append("Education (at least one school)")
    if missing:
        raise ProfileError("Please fill in: " + ", ".join(missing) + ".")
    if p["email"] and not _EMAIL.match(p["email"]):
        raise ProfileError("That email address doesn't look right.")
    return p


def is_complete(p: dict[str, Any] | None) -> bool:
    try:
        validate(p)
        return True
    except ProfileError:
        return False


def lines(text: str) -> list[str]:
    """Bullet points from a free-text box: one per line, list markers removed."""
    out = []
    for ln in (text or "").split("\n"):
        ln = re.sub(r"^\s*(?:[-*•·●▪◦]|\d+[.)、]|[（(]\d+[)）])\s*", "", ln).strip()
        if ln:
            out.append(ln)
    return out


def skill_lines(text: str) -> list[tuple[str, str]]:
    """``"Backend: Go (1.21), gRPC / HTTP"`` → [("Backend", "Go (1.21), gRPC / HTTP")]:
    one line each, kept exactly as typed; a short "Label:" prefix is the label."""
    out = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^([^:：,，;；]{1,30}?)\s*[:：]\s*(.+)$", ln)
        out.append((m.group(1).strip(), m.group(2).strip()) if m else ("", ln))
    return out


def skill_groups(text: str) -> list[tuple[str, list[str]]]:
    """``"Backend: Go, Python\\nDatabases: PostgreSQL"`` → [("Backend", [...]), …].
    Lines without a label go in a group with an empty label."""
    groups: list[tuple[str, list[str]]] = []
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        label, items = "", ln
        m = re.match(r"^([^:：,，、;；]{1,30})[:：]\s*(.+)$", ln)
        if m:
            label, items = m.group(1).strip(), m.group(2)
        parts = [x.strip() for x in re.split(r"[,，、;；/|]|\s{2,}", items) if x.strip()]
        if parts:
            groups.append((label, parts))
    return groups


_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                              "nov", "dec"))}


def start_date(s: str) -> tuple[int, int] | None:
    """(year, month) from how people write a start date: 2021.07, 2021-7,
    2021年7月, Jul 2021, 2021. None when there's no year."""
    s = (s or "").strip().lower()
    m = re.search(r"((?:19|20)\d{2})\s*[./\-年]\s*(\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+((?:19|20)\d{2})", s)
    if m and m.group(1) in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1)]
    m = re.search(r"((?:19|20)\d{2})", s)
    return (int(m.group(1)), 0) if m else None


def newest_first(p: dict[str, Any]) -> dict[str, Any]:
    """The profile with work experience newest first, by start date (what
    recruiters and applicant-tracking systems expect). Left as entered when any
    job has no readable start date."""
    jobs = p.get("experience") or []
    keys = [start_date(j.get("start", "")) for j in jobs]
    if len(jobs) < 2 or any(k is None for k in keys):
        return p
    order = sorted(range(len(jobs)), key=lambda i: keys[i], reverse=True)
    return {**p, "experience": [jobs[i] for i in order]}


def facts_text(p: dict[str, Any]) -> str:
    """The profile as plain text, for the AI's fit check. Leaves out phone,
    email, birth date and gender: the AI doesn't need them."""
    out = [f"Name: {p.get('name', '')}", f"Job title: {p.get('job_title', '')}"]
    if p.get("focus"):
        out.append(f"Focus: {p['focus']}")
    if p.get("location"):
        out.append(f"Location: {p['location']}")
    if p.get("summary"):
        out.append(f"About me:\n{p['summary']}")
    if p.get("skills"):
        out.append(f"Skills:\n{p['skills']}")
    for section, title in (("experience", "Work experience"), ("projects", "Projects"),
                           ("education", "Education"), ("languages", "Languages")):
        rows = p.get(section) or []
        if not rows:
            continue
        out.append(f"{title}:")
        for r in rows:
            head = " | ".join(v for k, v in r.items() if k != "details" and v)
            out.append(f"- {head}")
            for b in lines(r.get("details", "")):
                out.append(f"    • {b}")
    return "\n".join(out)
