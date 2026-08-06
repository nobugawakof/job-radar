"""Delivery — Telegram bot and the shared delivery service.

Both delivery channels (Telegram digest and the web dashboard) reflect the same
underlying data (FR-30); the dashboard reads the DB directly, while this package
handles the push side. Delivery is durable and retried so no posting that
passed filtering is ever lost to a Telegram outage (NFR-5).
"""

from .digest import build_digest, format_posting_line  # noqa: F401
from .telegram import DeliveryService, TelegramTransport, TelegramBot  # noqa: F401
