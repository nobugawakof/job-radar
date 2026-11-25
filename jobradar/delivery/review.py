"""The batched Telegram prompt for the review queue (SRS FR-22, FR-23, FR-26).

One message per check lists the jobs that don't say where they hire, each with
a ✅ / ❌ button pair: one tap decides (FR-23). Pressing a button removes that
job's row from the message.
"""

from __future__ import annotations

from typing import Any

from .digest import _esc

MAX_PER_MESSAGE = 10


def prompt(items: list[tuple[int, dict[str, Any]]], total_pending: int) -> tuple[str, dict[str, Any]]:
    """(HTML text, inline keyboard) for up to MAX_PER_MESSAGE (id, posting) items."""
    shown = items[:MAX_PER_MESSAGE]
    lines = [f"🗂 <b>{len(items)} job{'s' if len(items) != 1 else ''} don't say where they hire.</b>",
             "Can you work from where you are? ✅ sends you the job, ❌ skips it.", ""]
    rows = []
    for n, (item_id, p) in enumerate(shown, 1):
        title = _esc((p.get("title") or "(untitled)")[:120])
        src = _esc(", ".join(p.get("origins") or [p.get("source") or ""]))
        url = p.get("source_url") or ""
        lines.append(f"{n}. <b>{title}</b> — {src}" + (f"\n    {_esc(url)}" if url else ""))
        rows.append([{"text": f"✅ {n}", "callback_data": f"rv:{item_id}:y"},
                     {"text": f"❌ {n}", "callback_data": f"rv:{item_id}:n"}])
    more = total_pending - len(shown)
    if more > 0:
        lines += ["", f"…and {more} more in the dashboard (Review tab)."]
    return "\n".join(lines), {"inline_keyboard": rows}


def without_item(markup: dict[str, Any] | None, item_id: int) -> dict[str, Any]:
    """The keyboard with one job's buttons removed (after it's decided)."""
    rows = (markup or {}).get("inline_keyboard") or []
    keep = [r for r in rows if not any(str(b.get("callback_data", "")).startswith(f"rv:{item_id}:") for b in r)]
    return {"inline_keyboard": keep}
