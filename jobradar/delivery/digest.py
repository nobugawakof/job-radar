"""Digest formatting.

FR-31: Telegram delivery is batched into one digest per run, never one message
per posting. IR-4: a digest line must be readable on a phone without expansion
— title, salary if known, source, and link. NFR-12: an empty digest is never
sent; silence means nothing matched.
"""

from __future__ import annotations

from typing import Any


# Telegram hard-limits a message to 4096 chars; keep a margin for batching.
MAX_MESSAGE_CHARS = 3500


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


def format_posting_line(posting: dict[str, Any], index: int | None = None) -> str:
    """One phone-readable line per posting (IR-4)."""
    prefix = f"{index}. " if index is not None else "• "
    title = posting.get("title") or "(untitled)"
    parts = [f"{prefix}<b>{_esc(title)}</b>"]
    sal = format_salary(posting)
    if sal:
        parts.append(f"   💰 {_esc(sal)}")
    origins = posting.get("origins") or [posting.get("source")]
    src = ", ".join(o for o in origins if o)  # FR-29: show all merged origins
    tier = posting.get("source_tier", "")
    remote = posting.get("is_remote")
    meta = f"   📡 {_esc(src)} [{tier}]"
    if remote and remote != "unknown":
        meta += f" · {remote}"
    parts.append(meta)
    url = posting.get("source_url")
    if url:
        parts.append(f"   🔗 {_esc(url)}")  # FR-32: link back to the original
    return "\n".join(parts)


def build_digest(postings: list[dict[str, Any]]) -> list[str]:
    """Return one or more digest messages, or an empty list if nothing to send.

    Multiple messages are only produced when a single run's digest exceeds
    Telegram's length limit — it is still one digest per run, chunked for
    transport, not one message per posting. An empty run sends nothing.
    """
    if not postings:
        return []  # never send an empty digest.

    header = f"🛰️ <b>Job Radar</b> — {len(postings)} new posting(s)"
    lines = [format_posting_line(p, i + 1) for i, p in enumerate(postings)]

    messages: list[str] = []
    buf = header
    for line in lines:
        candidate = f"{buf}\n\n{line}"
        if len(candidate) > MAX_MESSAGE_CHARS:
            messages.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        messages.append(buf)
    return messages


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
