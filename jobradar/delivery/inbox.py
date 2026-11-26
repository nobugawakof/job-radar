"""The bot's inbox: the one place that reads Telegram updates for your bot.

Telegram hands a bot its updates (messages, channel posts, button presses)
through ``getUpdates``. Unconfirmed updates are returned oldest first, 100 at a
time, so a reader that never confirms them sees the same 100 forever, and two
readers of the same bot step on each other ("409 Conflict"). Here a single
inbox per bot reads and *confirms* updates, keeping its place (the update
offset) in the database, and files what it sees:

* chats that wrote to the bot → remembered for "Find my chat";
* posts from channels the bot is in → kept 14 days for the Telegram channel
  source (when your own account isn't logged in);
* button presses → passed to the registered handler (e.g. the review queue).

The dashboard and ``serve`` poll it every few seconds in the background, so
buttons respond quickly; ``jobradar run`` polls it once at the start.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from ..redact import redact

log = logging.getLogger("jobradar.inbox")

Handler = Callable[[dict[str, Any]], "str | None"]  # callback_query -> toast text


class InboxError(RuntimeError):
    pass


def _iso_from_unix(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


class BotInbox:
    def __init__(self, token: str, state, *, timeout: float = 20.0):
        self.token = token
        self.state = state
        self.timeout = timeout
        self.bot_id = token.split(":", 1)[0]
        self._lock = threading.Lock()
        self._handlers: dict[str, Handler] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- calls
    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                       for k, v in (params or {}).items()}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                desc = json.loads(e.read()).get("description", "")
            except (ValueError, AttributeError):
                desc = ""
            raise InboxError(redact(f"Telegram {method} {e.code}: {desc}")) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise InboxError(redact(f"Telegram {method}: {getattr(e, 'reason', e)}")) from e
        if not payload.get("ok"):
            raise InboxError(redact(f"Telegram {method}: {payload.get('description')}"))
        return payload.get("result")

    def on_start(self, handler: Callable[[dict[str, Any], str], "str | None"]) -> None:
        """Handle "/start CODE" in a private chat (linking a user's chat); the
        returned text is sent back to that chat."""
        self._start_handler = handler

    def on_button(self, prefix: str, handler: Handler) -> None:
        """Handle presses of inline buttons whose callback data starts with ``prefix``."""
        self._handlers[prefix] = handler

    # -------------------------------------------------------------- poll
    def poll(self) -> int:
        """Fetch, file and confirm pending updates. Returns how many."""
        with self._lock:
            key = f"bot_offset:{self.bot_id}"
            offset = int(self.state.meta_get(key) or 0)
            updates = self.call("getUpdates", {
                "offset": offset, "timeout": 0, "limit": 100,
                "allowed_updates": ["message", "channel_post", "callback_query", "my_chat_member"],
            }) or []
            for upd in updates:
                try:
                    self._file(upd)
                except Exception:  # noqa: BLE001 - one odd update must not block the rest
                    log.warning("couldn't handle a Telegram update", exc_info=True)
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
            if updates:
                self.state.meta_set(key, str(offset))  # confirms them with Telegram on the next call
            return len(updates)

    def _file(self, upd: dict[str, Any]) -> None:
        for kind in ("message", "my_chat_member"):
            chat = (upd.get(kind) or {}).get("chat")
            if chat:
                self.state.remember_chat(chat)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        handler = getattr(self, "_start_handler", None)
        if handler and text.startswith("/start") and (msg.get("chat") or {}).get("type") == "private":
            code = text.split(maxsplit=1)[1].strip() if " " in text else ""
            reply = handler(msg["chat"], code)
            if reply:
                try:
                    self.call("sendMessage", {"chat_id": msg["chat"]["id"], "text": reply}, timeout=10)
                except InboxError:
                    pass
        post = upd.get("channel_post")
        if post:
            chat = post.get("chat") or {}
            self.state.remember_chat(chat)
            text = (post.get("text") or post.get("caption") or "").strip()
            if text and post.get("message_id") and post.get("date"):
                self.state.add_bot_post(str(chat.get("id")), post["message_id"], chat.get("username") or "",
                                        _iso_from_unix(post["date"]), text)
        cq = upd.get("callback_query")
        if cq:
            self._button(cq)

    def _button(self, cq: dict[str, Any]) -> None:
        data = cq.get("data") or ""
        handler = next((h for p, h in self._handlers.items() if data.startswith(p)), None)
        note = None
        if handler is not None:
            try:
                note = handler(cq)
            except Exception as e:  # noqa: BLE001
                log.warning("button handler failed: %s", e)
                note = "Sorry, that didn't work. Try the dashboard."
        try:
            self.call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note or ""}, timeout=10)
        except InboxError:
            pass  # too old to answer; the decision was still recorded

    # ------------------------------------------------------ background
    def start(self, every: float = 3.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            backoff = every
            while not self._stop.is_set():
                try:
                    self.poll()
                    backoff = every
                except InboxError as e:
                    # 409: another program reads this bot right now; 401: bad token.
                    backoff = min(backoff * 2, 120)
                    log.debug("bot inbox: %s (retrying in %.0fs)", e, backoff)
                except Exception:  # noqa: BLE001
                    backoff = min(backoff * 2, 120)
                    log.debug("bot inbox failed", exc_info=True)
                self._stop.wait(backoff)

        self._stop.clear()
        self._thread = threading.Thread(target=loop, name="bot-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_INBOXES: dict[tuple[str, str], BotInbox] = {}
_INBOXES_LOCK = threading.Lock()


def inbox_for(token: str, state) -> BotInbox:
    """One inbox per (bot, database) in this process, shared by everything."""
    key = (token, str(getattr(state, "path", id(state))))
    with _INBOXES_LOCK:
        box = _INBOXES.get(key)
        if box is None or box.state is not state:
            box = BotInbox(token, state)
            if key in _INBOXES:
                box._handlers = _INBOXES[key]._handlers
                box._start_handler = getattr(_INBOXES[key], "_start_handler", None)
                _INBOXES[key].stop()
            _INBOXES[key] = box
        return box


def stop_all() -> None:
    with _INBOXES_LOCK:
        for box in _INBOXES.values():
            box.stop()
        _INBOXES.clear()
