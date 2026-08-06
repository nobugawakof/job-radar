"""Configuration.

A single TOML file for everything non-secret; secrets (the Telegram bot token,
Reddit OAuth) come from the environment and never touch the config file. This
is a single-user, Telegram-only build: one keyword set, one remote preference,
one destination chat.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    # What to look for.
    keywords: list[str] = field(default_factory=lambda: ["web3", "web2", "backend", "frontend", "ai", "fullstack"])
    remote_only: bool = True
    # Regions of interest (countries/cities/blocs, as you'd type them). Empty
    # means no region filter. A posting passes if it's remote-worldwide OR its
    # location names one of these, e.g. ["Hong Kong", "Malaysia", "Worldwide"].
    regions: list[str] = field(default_factory=list)

    # Where to send.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Optional AI extraction (off by default). When on, each posting about to be
    # sent is enriched by Claude into cleaner fields + responsibilities/
    # requirements. Needs JOBRADAR_ANTHROPIC_API_KEY in the environment.
    use_ai: bool = False
    ai_model: str = "claude-opus-5"      # set "claude-haiku-4-5" to cut cost
    ai_max_chars: int = 6000             # cap post text sent to the API
    anthropic_api_key: str | None = None

    # How it runs.
    state_path: str = "jobradar-state.json"
    run_interval_hours: float = 4.0
    catchup_lookback_hours: float = 72.0        # cap how far back a source looks
    default_request_interval_seconds: float = 5.0
    message_interval_seconds: float = 0.5       # gap between per-posting Telegram sends
    tier_b_failure_threshold: int = 3           # auto-disable a broken scraper
    max_sent_remembered: int = 5000             # bound the state file
    notify_owner_on_source_disable: bool = True

    @property
    def user_agent(self) -> str:
        from jobradar import __version__

        return f"SocialJobRadar/{__version__} (self-hosted; non-commercial)"


_KNOWN = set(Config.__dataclass_fields__)


def load(path: str | os.PathLike[str] | None = None) -> Config:
    data: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as fh:
                data = tomllib.load(fh)

    kwargs = {k: v for k, v in data.items() if k in _KNOWN}
    cfg = Config(**kwargs)

    # Secrets from the environment win and are never persisted here.
    token = os.environ.get("JOBRADAR_TELEGRAM_BOT_TOKEN") or data.get("telegram_bot_token")
    chat = os.environ.get("JOBRADAR_TELEGRAM_CHAT_ID") or data.get("telegram_chat_id")
    ai_key = os.environ.get("JOBRADAR_ANTHROPIC_API_KEY") or data.get("anthropic_api_key")
    if token:
        cfg = replace(cfg, telegram_bot_token=token)
    if chat:
        cfg = replace(cfg, telegram_chat_id=str(chat))
    if ai_key:
        cfg = replace(cfg, anthropic_api_key=ai_key)
    return cfg


def source_definitions(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    """Return the raw [[sources]] entries from the config file."""
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return list(data.get("sources", []))
