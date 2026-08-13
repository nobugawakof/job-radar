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
    # Sites to search, as simple URL lists (en = English, cn = Chinese). Both are
    # just source lists; the split is only for your own organisation. Each URL is
    # resolved to the right collector automatically (see jobradar/sites.py).
    en: list[str] = field(default_factory=list)
    cn: list[str] = field(default_factory=list)

    # What to look for.
    keywords: list[str] = field(default_factory=lambda: ["web3", "web2", "backend", "frontend", "ai", "fullstack"])
    # Role categories to always drop, even if they match a keyword (e.g.
    # marketing/sales/growth/design when you only want engineering roles).
    exclude_keywords: list[str] = field(default_factory=list)
    remote_only: bool = True
    # Salary filter (approximate). min_salary_usd drops postings whose parsed pay
    # is below this (rough annual-USD estimate); require_salary drops postings
    # with no stated salary at all. 0 / false = off.
    min_salary_usd: int = 0
    require_salary: bool = False
    # Regions of interest (countries/cities/blocs, as you'd type them). Empty
    # means no region filter. A posting passes if it's remote-worldwide OR its
    # location names one of these, e.g. ["Hong Kong", "Malaysia", "Worldwide"].
    regions: list[str] = field(default_factory=list)

    # Where to send.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Optional AI extraction (off by default). When on, each posting about to be
    # sent is enriched into cleaner fields + responsibilities/requirements.
    # Provider is "claude" (paid) or "gemini" (Google, has a free tier).
    use_ai: bool = False
    ai_provider: str = "claude"          # "claude" or "gemini"
    ai_model: str = "claude-opus-5"      # Claude: "claude-haiku-4-5" is cheaper; Gemini: "gemini-2.5-flash"
    ai_max_chars: int = 6000             # cap post text sent to the API
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None    # free key from https://aistudio.google.com/apikey

    # Source credentials — configured here (in the file), no env vars needed.
    # Only fill in the ones for sources you actually use.
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    x_bearer_token: str | None = None    # Twitter/X — requires a PAID X API plan

    # How it runs.
    state_path: str = "jobradar-state.json"
    run_interval_hours: float = 4.0
    catchup_lookback_hours: float = 72.0        # cap how far back a source looks
    first_run_lookback_hours: float = 168.0     # wider window the very first time a source runs
    max_messages_per_run: int = 25              # cap sends per run so a backlog doesn't flood
    max_posting_age_days: int = 30              # drop postings older than this (0 = no limit)
    default_request_interval_seconds: float = 5.0
    message_interval_seconds: float = 3.0       # gap between sends (Telegram flood-limits ~20/min per chat)
    tier_b_failure_threshold: int = 3           # auto-disable a broken scraper
    max_sent_remembered: int = 5000             # bound the state file
    notify_owner_on_source_disable: bool = True

    @property
    def user_agent(self) -> str:
        from jobradar import __version__

        return f"SocialJobRadar/{__version__} (self-hosted; non-commercial)"


_KNOWN = set(Config.__dataclass_fields__)


# Built-in sources used when the config file lists no [[sources]] of its own.
# These need no extra credentials, so a fresh config with just a Telegram token
# works out of the box. Add [[sources]] to the config to override this list.
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"name": "bluesky", "type": "bluesky", "tier": "A", "query": "hiring remote developer"},
    {"name": "hackernews", "type": "hn", "tier": "A", "classifier_prior": 1.0},
    {"name": "weworkremotely", "type": "rss", "tier": "A",
     "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
]


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
    # Secrets: config-file value is used as-is; an environment variable, if set,
    # overrides it (handy on servers, unnecessary for the desktop .exe).
    overrides = {
        "telegram_bot_token": os.environ.get("JOBRADAR_TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("JOBRADAR_TELEGRAM_CHAT_ID"),
        "anthropic_api_key": os.environ.get("JOBRADAR_ANTHROPIC_API_KEY"),
        "gemini_api_key": os.environ.get("JOBRADAR_GEMINI_API_KEY"),
        "reddit_client_id": os.environ.get("JOBRADAR_REDDIT_CLIENT_ID"),
        "reddit_client_secret": os.environ.get("JOBRADAR_REDDIT_CLIENT_SECRET"),
        "x_bearer_token": os.environ.get("JOBRADAR_X_BEARER_TOKEN"),
    }
    for field_name, env_value in overrides.items():
        value = env_value or data.get(field_name)
        if value:
            cfg = replace(cfg, **{field_name: str(value)})
    return cfg


def source_definitions(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    """Return the [[sources]] from the config file, or the built-in defaults.

    If the config lists no sources of its own, DEFAULT_SOURCES is used so the app
    works with nothing more than a Telegram token configured.
    """
    data: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as fh:
                data = tomllib.load(fh)
    return list(data.get("sources", []))


def build_sources(cfg: Config, path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    """Assemble the source list from the config.

    Priority: sites from the `en`/`cn` URL lists, plus any explicit `[[sources]]`
    blocks (for advanced/custom feeds). If none are configured, fall back to the
    built-in defaults so the app still works out of the box.
    """
    from .sites import resolve

    resolved = resolve(list(cfg.en) + list(cfg.cn))
    explicit = source_definitions(path)
    combined = resolved + explicit
    return combined if combined else [dict(s) for s in DEFAULT_SOURCES]
