"""Message formatting.

Each posting is sent as its own Telegram message, laid out like a structured
job card (emoji section headers + hashtags + apply info + source link), modelled
on the format the user requested. Sections are only rendered when we have the
data for them, so a sparse scraped post still produces a tidy card and a rich
one (e.g. a Telegram jobs-channel post) keeps its full detail.
"""

from __future__ import annotations

import re
from typing import Any

# Keep well under Telegram's 4096-char hard limit; the body is the only part
# that can be long.
MAX_BODY_CHARS = 2500


def format_salary(posting: dict[str, Any]) -> str | None:
    raw = posting.get("salary_raw")
    if raw:
        return raw.strip()
    lo, hi, cur = posting.get("salary_min"), posting.get("salary_max"), posting.get("salary_currency")
    if lo:
        sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur or "", "")
        if hi and hi != lo:
            return f"{sym}{int(lo):,}–{sym}{int(hi):,}"
        return f"{sym}{int(lo):,}"
    return None


def _arrangement_tags(posting: dict[str, Any]) -> str:
    remote = posting.get("is_remote")
    tags = []
    if posting.get("is_worldwide") or remote == "remote":
        tags.append("#远程")
    elif remote == "hybrid":
        tags.append("#混合办公")
    elif remote == "onsite":
        tags.append("#坐班")
    return " ".join(tags)


def _role_tags(posting: dict[str, Any]) -> str:
    kws = posting.get("matched_keywords") or []
    return " ".join(f"#{_esc(k)}" for k in kws)


def _clean_body(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS].rstrip() + " …"
    return _esc(text)


def format_message(posting: dict[str, Any]) -> str:
    """Build one structured job-card message for a single posting."""
    lines: list[str] = ["#招聘"]

    # 🏡 Title (company | role | …) with keyword hashtags.
    title = _esc(posting.get("title") or "(untitled)")
    role_tags = _role_tags(posting)
    head = f"🏡  <b>{title}</b>"
    if role_tags:
        head += f"    {role_tags}"
    lines.append(head)

    # 🛵 Work arrangement.
    arrangement = _arrangement_tags(posting)
    if arrangement:
        lines.append(f"🛵  合作方式：{arrangement}")

    # 💰 Compensation.
    sal = format_salary(posting)
    if sal:
        lines.append(f"💰  薪酬福利：{_esc(sal)}")

    # 📍 Location.
    if posting.get("is_worldwide"):
        lines.append("🌍  工作地点：全球远程 / Worldwide")
    elif posting.get("location"):
        lines.append(f"📍  工作地点：{_esc(str(posting['location']))}")

    # 🌱 Responsibilities / 🌵 Requirements — populated only when AI extraction
    # is on; otherwise the full post body carries them under 职位详情.
    resp = posting.get("responsibilities") or []
    reqs = posting.get("requirements") or []
    if resp:
        lines.append("")
        lines.append("🌱  岗位职责：")
        lines.extend(f"{i}️⃣ {_esc(r)}" if i <= 9 else f"• {_esc(r)}"
                     for i, r in enumerate(resp[:9], start=1))
    if reqs:
        lines.append("")
        lines.append("🌵  岗位要求：")
        lines.extend(f"{i}️⃣ {_esc(r)}" if i <= 9 else f"• {_esc(r)}"
                     for i, r in enumerate(reqs[:9], start=1))

    # 📝 Details: the original post body. Skipped when AI already split the post
    # into the responsibilities/requirements sections above.
    if not (resp or reqs):
        body = _clean_body(posting.get("description") or "")
        if body:
            lines.append("")
            lines.append("📝  职位详情：")
            lines.append(body)

    # 📮 How to apply.
    contact = posting.get("contact")
    if contact:
        lines.append("")
        lines.append(f"📮  申请方式：{_esc(str(contact))}")

    # 🚧 Source link back to the original post.
    url = posting.get("source_url")
    if url:
        origins = posting.get("origins") or [posting.get("source")]
        src = ", ".join(o for o in origins if o)
        tier = posting.get("source_tier", "")
        lines.append("")
        lines.append(f"🚧  岗位来源：{_esc(url)}")
        lines.append(f"📡  {_esc(src)} [{tier}]")

    return "\n".join(lines)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
