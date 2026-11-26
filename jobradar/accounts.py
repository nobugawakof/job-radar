"""What each account uses at run time.

The administrator (kof311, user 1) keeps everything as before: the settings in
config.toml, the Telegram session in ``telegram_session_path``, the bot chat in
``telegram_chat_id``, email and the uploaded résumé.

Every other user gets the shared search and the administrator's settings, but:

* their own Telegram account (``sessions/user-<id>.session``), logged in from
  the dashboard with the administrator's Telegram API ID/hash;
* their own bot chat (linked with ``/start CODE``) for alerts and notices;
* their own profile, résumés (``resumes/u<id>/``), applications, queue,
  cooldowns and conversations;
* Telegram only: email goes out from the administrator's mailbox, so it isn't
  used for other people's applications, and no uploaded-résumé fallback.
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Config
from .state import ADMIN_ID

LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") == "admin" and int(user.get("id", 0)) == ADMIN_ID


def session_path(config: Config, base_dir: Path, user: dict[str, Any]) -> Path:
    if int(user["id"]) == ADMIN_ID:
        p = Path(config.telegram_session_path).expanduser()
        return p if p.is_absolute() else Path(base_dir) / p
    return Path(base_dir) / "sessions" / f"user-{int(user['id'])}.session"


def session_file(path: Path) -> Path:
    return path if str(path).endswith(".session") else Path(str(path) + ".session")


def has_telegram(config: Config, base_dir: Path, user: dict[str, Any]) -> bool:
    return session_file(session_path(config, base_dir, user)).exists()


def bot_chat(config: Config, user: dict[str, Any]) -> str | None:
    if int(user["id"]) == ADMIN_ID:
        return str(config.telegram_chat_id) if config.telegram_chat_id else None
    return user.get("bot_chat_id") or None


def resume_owner(user: dict[str, Any]) -> str:
    """Sub-folder of resumes/ for this user's files ('' = the administrator's, as before)."""
    return "" if int(user["id"]) == ADMIN_ID else f"u{int(user['id'])}"


def user_config(config: Config, base_dir: Path, user: dict[str, Any]) -> Config:
    """The settings as one user's applications and conversations see them."""
    if int(user["id"]) == ADMIN_ID:
        return config
    return replace(config, telegram_session_path=str(session_path(config, base_dir, user)),
                   telegram_chat_id=user.get("bot_chat_id") or None, apply_channels=["telegram"],
                   resume_source="generated", resume_pdf_path="", resume_text_path="", applicant_name="",
                   smtp_host=None, smtp_user=None, smtp_password=None, smtp_from=None)


def new_link_code() -> str:
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(8))
