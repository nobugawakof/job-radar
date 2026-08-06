"""Telegram transport, delivery service, and bot.

The transport is abstracted behind a small interface so the bot and delivery
service can be driven by a fake in tests (and so a network outage is contained).
The real transport uses only urllib.

Delivery is durable: each outgoing message is a row in ``deliveries`` and is
retried until it succeeds (NFR-5). A posting is only marked *delivered* to a
user once its digest message actually goes out, so a Telegram outage never
loses a posting — it stays queued and still shows in the dashboard.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any, Protocol

from ..db import iso, utcnow
from ..models import (
    STATUS_DELIVERED,
    STATUS_DISMISSED,
    STATUS_NEW,
    STATUS_SAVED,
)
from ..repos import Store
from . import digest as digestmod

log = logging.getLogger("jobradar.telegram")


class Transport(Protocol):
    def send_message(
        self, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]: ...


class TelegramTransport:
    """Real Telegram Bot API transport (stdlib urllib, HTTPS only — NFR-9)."""

    def __init__(self, token: str, timeout: float = 20.0):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        data = urllib.parse.urlencode(
            {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
        ).encode()
        req = urllib.request.Request(f"{self.base}/{method}", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description')}")
        return payload.get("result", {})

    def send_message(self, chat_id, text, reply_markup=None):
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._post("sendMessage", params)

    def answer_callback(self, callback_id, text=""):
        self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset=None, timeout=0):
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._post("getUpdates", params)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Delivery service
# --------------------------------------------------------------------------- #
class DeliveryService:
    def __init__(self, store: Store, transport: Transport, *, max_attempts: int = 5):
        self.store = store
        self.transport = transport
        self.max_attempts = max_attempts

    def send_run_digests(self, run_id: int | None) -> int:
        """Queue a batched digest per user for the postings collected this run.

        Muted users are skipped (FR-33): their postings accumulate as ``new``
        and remain visible in the dashboard until they unmute.
        """
        queued = 0
        for user in self.store.list_users():
            if not user["telegram_chat_id"]:
                continue
            if self.store.is_muted(user["id"]):
                continue  # FR-33
            postings = self.store.pending_delivery(user["id"])
            if postings:
                messages = digestmod.build_digest(user, postings)
                if messages:  # NFR-12
                    self.store.enqueue_delivery(
                        user["id"], run_id,
                        {"type": "digest", "chat_id": user["telegram_chat_id"],
                         "messages": messages,
                         "posting_ids": [p["id"] for p in postings]},
                    )
                    queued += 1
            # FR-22/26: batched review prompt for undetermined-geography items.
            review = self.store.review_queue(user["id"])
            if review:
                text, markup = _build_review_batch(review)
                self.store.enqueue_delivery(
                    user["id"], run_id,
                    {"type": "review", "chat_id": user["telegram_chat_id"],
                     "messages": [text], "reply_markup": markup},
                )
                queued += 1
        return queued

    def deliver_pending(self) -> dict[str, int]:
        """Flush the durable delivery queue with retry (NFR-5)."""
        sent = failed = 0
        for d in self.store.pending_deliveries(self.max_attempts):
            payload = d["payload"]
            chat_id = payload.get("chat_id")
            try:
                for i, msg in enumerate(payload.get("messages", [])):
                    markup = payload.get("reply_markup") if i == len(payload["messages"]) - 1 else None
                    self.transport.send_message(chat_id, msg, reply_markup=markup)
                self.store.mark_delivery_result(d["id"], ok=True)
                # Only now mark the postings delivered (NFR-5).
                if payload.get("type") == "digest" and payload.get("posting_ids"):
                    self.store.mark_delivered(d["user_id"], payload["posting_ids"])
                sent += 1
            except Exception as e:  # noqa: BLE001 - failures are retried, not fatal
                self.store.mark_delivery_result(d["id"], ok=False, error=str(e))
                log.warning("delivery %s failed (will retry): %s", d["id"], e)
                failed += 1
        return {"sent": sent, "failed": failed}


def _build_review_batch(items: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """One batched message + inline keyboard for the review queue (FR-22/26)."""
    lines = ["🕵️ <b>Review queue</b> — location unclear, confirm relevance:"]
    keyboard: list[list[dict[str, str]]] = []
    for i, p in enumerate(items[:10], start=1):
        title = (p.get("title") or "(untitled)")[:80]
        src = ", ".join(p.get("origins") or [p.get("source")])
        lines.append(f"\n<b>{i}. {digestmod._esc(title)}</b>\n   {digestmod._esc(src)}")
        excerpt = " ".join((p.get("description") or "").split())[:180]
        lines.append(f"   <i>{digestmod._esc(excerpt)}</i>")
        pid = p["id"]
        keyboard.append([
            {"text": f"✅ Relevant #{i}", "callback_data": f"rev:{pid}:yes"},
            {"text": f"❌ Not #{i}", "callback_data": f"rev:{pid}:no"},
        ])
    return "\n".join(lines), {"inline_keyboard": keyboard}


# --------------------------------------------------------------------------- #
# Bot (command + callback handling)
# --------------------------------------------------------------------------- #
class TelegramBot:
    """Handles inbound updates: linking, review resolution, settings (IR-1-4)."""

    def __init__(self, store: Store, transport: Transport):
        self.store = store
        self.transport = transport

    # ---- entry point ------------------------------------------------------
    def process_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def poll_once(self, offset_key: str = "telegram.update_offset") -> int:
        """Fetch and process a batch of updates; returns count handled."""
        offset = self.store.db.get_meta(offset_key)
        updates = self.transport.get_updates(int(offset) + 1 if offset else None)
        handled = 0
        for upd in updates:
            self.process_update(upd)
            self.store.db.set_meta(offset_key, str(upd["update_id"]))
            handled += 1
        return handled

    # ---- messages ---------------------------------------------------------
    def _on_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat", {}).get("id"))
        text = (message.get("text") or "").strip()
        if not text:
            return
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        # Linking (IR-1): "/start <code>" or a bare code before linking.
        if cmd in ("/start", "/link"):
            code = args[0] if args else ""
            return self._link(chat_id, code)

        user = self.store.get_user_by_chat_id(chat_id)
        if not user:
            # Maybe they pasted a bare linking code.
            if self.store.link_telegram(text, chat_id):
                return self._reply(chat_id, "✅ Linked! Send /status to see your settings.")
            return self._reply(chat_id, "Send the one-time code your admin gave you to link, "
                                        "or /start <code>.")

        if cmd == "/status":
            return self._status(user, chat_id)
        if cmd == "/review":
            return self._send_review(user, chat_id)
        if cmd == "/digest":
            return self._send_digest(user, chat_id)
        if cmd == "/mute":
            hours = int(args[0]) if args and args[0].isdigit() else 24
            self.store.update_user(user["id"], muted_until=utcnow() + timedelta(hours=hours))
            return self._reply(chat_id, f"🔇 Muted for {hours}h. Postings still collect.")
        if cmd == "/unmute":
            self.store.update_user(user["id"], muted_until=None)
            return self._reply(chat_id, "🔔 Unmuted.")
        if cmd == "/keywords":
            return self._edit_keywords(user, chat_id, args)
        if cmd == "/countries":
            return self._edit_countries(user, chat_id, args)

        return self._reply(chat_id, self._menu_text(), self._menu_markup())

    # ---- callbacks --------------------------------------------------------
    def _on_callback(self, cb: dict[str, Any]) -> None:
        data = cb.get("data", "")
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id"))
        user = self.store.get_user_by_chat_id(chat_id)
        cb_id = cb.get("id", "")
        if not user:
            return self.transport.answer_callback(cb_id, "Not linked.")

        if data.startswith("rev:"):
            _, pid, decision = data.split(":", 2)
            return self._resolve_review(user, pid, decision, cb_id, chat_id)
        if data == "menu:status":
            self.transport.answer_callback(cb_id)
            return self._status(user, chat_id)
        if data == "menu:review":
            self.transport.answer_callback(cb_id)
            return self._send_review(user, chat_id)
        if data == "menu:digest":
            self.transport.answer_callback(cb_id)
            return self._send_digest(user, chat_id)
        if data == "menu:unmute":
            self.store.update_user(user["id"], muted_until=None)
            return self.transport.answer_callback(cb_id, "Unmuted.")
        self.transport.answer_callback(cb_id)

    def _resolve_review(self, user, pid, decision, cb_id, chat_id) -> None:
        """FR-23: resolve a queued posting with a single interaction."""
        posting = self.store.get_posting(pid)
        source = posting["source"] if posting else None
        if decision == "yes":
            # Relevant → deliver it (becomes a normal new posting).
            self.store.set_user_posting_status(user["id"], pid, STATUS_NEW, resolved=True)
            self.store.record_resolution(user["id"], pid, "relevant", source)
            self.transport.answer_callback(cb_id, "Kept ✅")
        else:
            self.store.set_user_posting_status(user["id"], pid, STATUS_DISMISSED, resolved=True)
            self.store.record_resolution(user["id"], pid, "not_relevant", source)
            self.transport.answer_callback(cb_id, "Dismissed ❌")
        # FR-24: surface a suggested rule if a pattern of rejections emerges.
        for rule in self.store.suggested_rules(user["id"]):
            self._reply(chat_id, f"💡 Suggestion: {rule['suggestion']}")
            break

    # ---- helpers ----------------------------------------------------------
    def _link(self, chat_id: str, code: str) -> None:
        if code and self.store.link_telegram(code, chat_id):
            return self._reply(chat_id, "✅ Linked! Send /status to see your settings.")
        existing = self.store.get_user_by_chat_id(chat_id)
        if existing:
            return self._reply(chat_id, self._menu_text(), self._menu_markup())
        return self._reply(chat_id, "Welcome to Job Radar. Send the one-time code your admin "
                                    "gave you, or /start <code> to link.")

    def _status(self, user: dict[str, Any], chat_id: str) -> None:
        pending = len(self.store.pending_delivery(user["id"]))
        review = len(self.store.review_queue(user["id"]))
        muted = "yes" if self.store.is_muted(user["id"]) else "no"
        text = (
            f"👤 <b>{digestmod._esc(user['name'])}</b>\n"
            f"Keywords: {', '.join(user['keywords']) or '(none)'}\n"
            f"Eligible countries: {', '.join(user['eligible_countries']) or '(any)'}\n"
            f"Remote only: {'yes' if user['remote_only'] else 'no'}\n"
            f"Muted: {muted}\n"
            f"New postings: {pending} · Review queue: {review}"
        )
        self._reply(chat_id, text, self._menu_markup())

    def _send_digest(self, user: dict[str, Any], chat_id: str) -> None:
        postings = self.store.pending_delivery(user["id"])
        messages = digestmod.build_digest(user, postings)
        if not messages:  # NFR-12
            return self._reply(chat_id, "Nothing new right now.")
        for m in messages:
            self._reply(chat_id, m)
        self.store.mark_delivered(user["id"], [p["id"] for p in postings])

    def _send_review(self, user: dict[str, Any], chat_id: str) -> None:
        items = self.store.review_queue(user["id"])
        if not items:
            return self._reply(chat_id, "Review queue is empty. ✅")
        text, markup = _build_review_batch(items)
        self._reply(chat_id, text, markup)

    def _edit_keywords(self, user: dict[str, Any], chat_id: str, args: list[str]) -> None:
        kws = list(user["keywords"])
        if len(args) >= 2 and args[0] in ("add", "remove"):
            kw = args[1].lower()
            if args[0] == "add" and kw not in kws:
                kws.append(kw)
            elif args[0] == "remove" and kw in kws:
                kws.remove(kw)
            self.store.update_user(user["id"], keywords=kws)
        self._reply(chat_id, f"Keywords: {', '.join(kws) or '(none)'}\n"
                             f"Use /keywords add <word> or /keywords remove <word>.")

    def _edit_countries(self, user: dict[str, Any], chat_id: str, args: list[str]) -> None:
        from .. import geo

        if len(args) >= 2 and args[0] in ("add", "remove"):
            codes = list(user["eligible_countries"])
            new = geo.normalise_country_list([args[1]])
            if args[0] == "add":
                for c in new:
                    if c not in codes:
                        codes.append(c)
            else:
                codes = [c for c in codes if c not in new]
            self.store.update_user(user["id"], eligible_countries=codes)
            user = self.store.get_user(user["id"])
        self._reply(chat_id, f"Eligible countries: {', '.join(user['eligible_countries']) or '(any)'}\n"
                             f"Use /countries add <country> or /countries remove <country>.")

    def _menu_text(self) -> str:
        return "🛰️ <b>Job Radar</b> — choose an action:"

    def _menu_markup(self) -> dict[str, Any]:
        # IR-3: routine interactions via inline buttons, no typed commands needed.
        return {"inline_keyboard": [
            [{"text": "📰 Latest digest", "callback_data": "menu:digest"},
             {"text": "🕵️ Review queue", "callback_data": "menu:review"}],
            [{"text": "ℹ️ Status", "callback_data": "menu:status"},
             {"text": "🔔 Unmute", "callback_data": "menu:unmute"}],
        ]}

    def _reply(self, chat_id: str, text: str, markup: dict[str, Any] | None = None) -> None:
        self.transport.send_message(chat_id, text, reply_markup=markup)
