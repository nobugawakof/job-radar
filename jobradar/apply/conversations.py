"""Conversations with representatives (HR, recruiters), one per account.

The real conversation lives in your Telegram account; Job Radar mirrors it so
the dashboard can show it per representative and you can reply from there:

* every application Job Radar sends is recorded in that person's
  conversation (see Applier._record_conversation);
* :func:`sync_telegram` reads each conversation back from your account —
  their replies, and anything you sent them yourself from your phone — and
  sends you a "💬 replied" notice for each new reply;
* :func:`send_reply` sends a reply from the dashboard (Telegram from your
  account, or email), recorded in the same conversation.

Email replies aren't read: that would need access to your mailbox (IMAP).
"""

from __future__ import annotations

import logging
from datetime import timezone
from pathlib import Path
from typing import Any, Callable

from ..delivery.digest import _esc
from ..redact import mask_contact
from . import language

log = logging.getLogger("jobradar.conversations")

SYNC_LIMIT = 50  # newest messages read per conversation per sync


class ReplyError(RuntimeError):
    """A reply couldn't be sent (message is shown to the user)."""


def _session_ready(config) -> bool:
    session = str(config.telegram_session_path or "")
    path = session if session.endswith(".session") else session + ".session"
    return bool(config.telegram_api_id and config.telegram_api_hash and Path(path).exists())


def sync_telegram(config, state, *, notify: Callable[[str], None] | None = None,
                  lock_timeout: float = 15.0) -> dict[str, Any]:
    """Read new messages of every Telegram conversation from your account."""
    convs = state.conversations("telegram")
    if not convs:
        return {"synced": 0, "new_replies": 0}
    if not _session_ready(config):
        return {"synced": 0, "new_replies": 0, "error": "Telegram account not logged in"}
    from ..tgclient import ACCOUNT_LOCK, LoopThread, TelethonMissing, make_client

    if not ACCOUNT_LOCK.acquire(timeout=lock_timeout):
        return {"synced": 0, "new_replies": 0, "error": "Telegram account busy; try again shortly"}
    runner = LoopThread("tg-sync")
    client = None
    replies: list[tuple[dict[str, Any], str]] = []
    synced = 0
    try:
        try:
            client = make_client(runner, config.telegram_session_path, config.telegram_api_id,
                                 config.telegram_api_hash)
        except TelethonMissing as e:
            return {"synced": 0, "new_replies": 0, "error": str(e)}
        runner.call(lambda: client.connect(), 30)
        if not runner.call(lambda: client.is_user_authorized(), 15):
            return {"synced": 0, "new_replies": 0, "error": "Telegram account not logged in"}
        for conv in convs:
            min_id = state.last_telegram_message_id(conv["id"])
            targets = [int(conv["peer_id"])] if conv.get("peer_id") else []
            targets.append(conv["contact"])

            async def read(target):
                out = []
                async for m in client.iter_messages(target, limit=SYNC_LIMIT, min_id=min_id):
                    f = getattr(m, "file", None)
                    name = (getattr(f, "name", None) or ("photo" if getattr(m, "photo", None) else "file")) if f else ""
                    out.append((int(m.id), m.date, bool(m.out), getattr(m, "message", None) or "", name))
                return out

            msgs = None
            for target in targets:  # by id first (stable), then by @handle
                try:
                    msgs = runner.call(lambda: read(target), 60)
                    break
                except Exception as e:  # noqa: BLE001 - try the next way, then move on
                    log.debug("couldn't read conversation %s via %r: %s", mask_contact(conv["contact"]), target, e)
            if msgs is None:
                continue
            synced += 1
            for mid, date, out, text, fname in reversed(msgs):
                if date is not None and date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                new = state.add_message(conv["id"], "out" if out else "in", text, sent_at=date,
                                        external_id=str(mid), attachment=fname, unread=not out)
                if new and not out:
                    replies.append((conv, text or (f"📎 {fname}" if fname else "")))
    except TimeoutError as e:
        return {"synced": synced, "new_replies": len(replies), "error": str(e)}
    finally:
        if client is not None:
            try:
                runner.call(lambda: client.disconnect(), 10)
            except Exception:  # noqa: BLE001
                pass
        runner.stop()
        ACCOUNT_LOCK.release()
    if notify:
        for conv, text in replies:
            try:
                notify(reply_notice(config, conv, text))
            except Exception:  # noqa: BLE001
                log.debug("reply notice failed", exc_info=True)
    if replies:
        log.info("%d new repl%s from representatives", len(replies), "y" if len(replies) == 1 else "ies")
    return {"synced": synced, "new_replies": len(replies)}


def reply_notice(config, conv: dict[str, Any], text: str) -> str:
    lang = language.ui(config.message_language)
    who = conv.get("display_name") or conv.get("contact") or "?"
    job = language.t("reply_job", lang, title=_esc(conv["job_title"])) if conv.get("job_title") else ""
    excerpt = text if len(text) <= 600 else text[:600] + "…"
    return language.t("reply", lang, who=_esc(who), job=job, text=_esc(excerpt))


def send_reply(config, state, base_dir: Path, conversation_id: int, text: str) -> dict[str, Any]:
    """Send a reply in a conversation (Telegram from your account, or email)."""
    from .engine import build_senders
    from .senders import ChannelUnavailable, ContactError

    text = (text or "").strip()
    if not text:
        raise ReplyError("Write a message first.")
    if len(text) > 4000:
        raise ReplyError("That message is too long for Telegram (4,000 characters max).")
    conv = state.conversation(conversation_id)
    if conv is None:
        raise ReplyError("That conversation doesn't exist.")
    sender = build_senders(config, base_dir).get(conv["channel"])
    if sender is None:
        need = ("log in your Telegram account (Auto-apply tab)" if conv["channel"] == "telegram"
                else "set up email (Auto-apply tab)")
        raise ReplyError(f"To reply, {need}.")
    subject = ""
    if conv["channel"] == "email":
        last = next((m["subject"] for m in reversed(conv["messages"]) if m.get("subject")), "")
        subject = last if last.lower().startswith("re:") else f"Re: {last}" if last else "Re:"
    target = conv["contact"]
    try:
        result = sender.send(target, message=text, subject=subject, attachment=None)
    except (ContactError, ChannelUnavailable) as e:
        raise ReplyError(str(e)) from e
    finally:
        sender.close()
    state.add_message(conversation_id, "out", text, subject=subject,
                      external_id=getattr(result, "text_id", None))
    if getattr(result, "peer_id", None) and not conv.get("peer_id"):
        state.conversation_id(conv["channel"], conv["contact"], peer_id=result.peer_id,
                              display_name=getattr(result, "display_name", "") or None)
    return state.conversation(conversation_id)
