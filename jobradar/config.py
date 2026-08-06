"""Configuration loading.

Configuration is data, not code (SR-1, NFR-14). The runtime reads a single
TOML file plus environment variables for secrets. Secrets (API tokens) are
never stored in the config file that lives in version control (DR-6): they are
read from the environment or an untracked secrets file referenced by path.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


# Sensible defaults straight from the SRS. Every value here is overridable.
DEFAULTS: dict[str, Any] = {
    "run_interval_hours": 4,          # FR-1
    "catchup_lookback_hours": 72,     # FR-5
    "non_posting_retention_days": 7,  # FR-9
    "posting_retention_days": 90,     # DR-3
    "review_expiry_days": 14,         # FR-25
    "tier_b_failure_threshold": 3,    # SR-4
    "default_request_interval_seconds": 5,  # SR-6
    "reddit_qpm_limit": 100,          # C-5 / NFR-3
    "web_host": "127.0.0.1",          # IR-8: localhost by default
    "web_port": 8080,
    "backup_dir": "backups",          # NFR-7
    "backup_interval_hours": 24,      # NFR-7
    # Defaults applied to a newly created member (FR-13, FR-15).
    "default_keywords": ["web3", "web2", "backend", "frontend", "ai", "fullstack"],
    "default_remote_only": True,
    "default_eligible_countries": [],  # Q-1 is owner-supplied; empty == unset
}


@dataclass(frozen=True)
class Config:
    db_path: str = "jobradar.db"
    run_interval_hours: int = DEFAULTS["run_interval_hours"]
    catchup_lookback_hours: int = DEFAULTS["catchup_lookback_hours"]
    non_posting_retention_days: int = DEFAULTS["non_posting_retention_days"]
    posting_retention_days: int = DEFAULTS["posting_retention_days"]
    review_expiry_days: int = DEFAULTS["review_expiry_days"]
    tier_b_failure_threshold: int = DEFAULTS["tier_b_failure_threshold"]
    default_request_interval_seconds: float = DEFAULTS["default_request_interval_seconds"]
    reddit_qpm_limit: int = DEFAULTS["reddit_qpm_limit"]
    web_host: str = DEFAULTS["web_host"]
    web_port: int = DEFAULTS["web_port"]
    backup_dir: str = DEFAULTS["backup_dir"]
    backup_interval_hours: int = DEFAULTS["backup_interval_hours"]
    default_keywords: list[str] = field(default_factory=lambda: list(DEFAULTS["default_keywords"]))
    default_remote_only: bool = DEFAULTS["default_remote_only"]
    default_eligible_countries: list[str] = field(default_factory=lambda: list(DEFAULTS["default_eligible_countries"]))
    # Secrets are looked up here, never persisted to the DB or config file.
    telegram_bot_token: str | None = None

    @property
    def user_agent(self) -> str:
        # SR-5: identify honestly, do not spoof.
        return f"SocialJobRadar/{_version()} (self-hosted; non-commercial)"


def _version() -> str:
    from jobradar import __version__

    return __version__


def load(path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration from a TOML file, with env-var secret overrides.

    Missing files yield an all-defaults config, which is enough to run the
    collector against unauthenticated sources (Bluesky, HN, RSS).
    """
    data: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as fh:
                data = tomllib.load(fh)

    # Only keep keys the dataclass knows about; ignore [sources] etc. which are
    # consumed elsewhere.
    known = {f for f in Config.__dataclass_fields__}
    kwargs = {k: v for k, v in data.items() if k in known}

    cfg = Config(**kwargs)

    # Secrets from environment take precedence and never touch disk here (DR-6).
    token = os.environ.get("JOBRADAR_TELEGRAM_BOT_TOKEN") or data.get("telegram_bot_token")
    if token:
        cfg = replace(cfg, telegram_bot_token=token)

    return cfg


def source_definitions(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    """Return the raw [[sources]] entries from the config file, if any.

    A source definition is a plain dict; :func:`jobradar.collectors.registry.build`
    turns it into a live collector. Keeping these as data satisfies SR-1.
    """
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    return list(data.get("sources", []))
