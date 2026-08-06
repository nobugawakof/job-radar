"""Delivery — Telegram send-only."""

from .digest import build_digest, format_posting_line  # noqa: F401
from .telegram import TelegramTransport, send_all  # noqa: F401
