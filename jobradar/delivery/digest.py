"""Message formatting.

Each posting is sent as its own Telegram message (the user asked for separate
messages, not a batched digest). A message is phone-readable at a glance:
title, salary if known, location if known, source(s), and a link back to the
original post.
"""

from __future__ import annotations

from typing import Any


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


def format_message(posting: dict[str, Any]) -> str:
    """One self-contained Telegram message for a single posting."""
    title = posting.get("title") or "(untitled)"
    lines = [f"🛰️ <b>{_esc(title)}</b>"]

    sal = format_salary(posting)
    if sal:
        lines.append(f"💰 {_esc(sal)}")

    loc = posting.get("location")
    if posting.get("is_worldwide"):
        lines.append("🌍 Remote — worldwide")
    elif loc:
        lines.append(f"📍 {_esc(str(loc))}")

    origins = posting.get("origins") or [posting.get("source")]
    src = ", ".join(o for o in origins if o)  # show all merged origins
    tier = posting.get("source_tier", "")
    remote = posting.get("is_remote")
    meta = f"📡 {_esc(src)} [{tier}]"
    if remote and remote != "unknown":
        meta += f" · {remote}"
    lines.append(meta)

    kws = posting.get("matched_keywords") or []
    if kws:
        lines.append("🏷️ " + ", ".join(_esc(k) for k in kws))

    url = posting.get("source_url")
    if url:
        lines.append(f"🔗 {_esc(url)}")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
