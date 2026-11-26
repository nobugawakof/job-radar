"""Read, validate and safely write ``config.toml`` for the dashboard.

Python's standard library reads TOML but can't write it, so this module has a
small writer for exactly the value types the config uses (strings, numbers,
booleans, lists, and ``[[sources]]`` tables). Writes are paranoid:

* every value is validated and coerced against the :class:`Config` field type;
* the new file is parsed back and compared with what we meant to write before
  it replaces the old one (atomic rename), so a writer bug can't corrupt it;
* the original file is copied to ``config.toml.bak`` the first time the
  dashboard saves, since hand-written comments are not preserved;
* keys the dashboard doesn't know about and ``[[sources]]`` blocks are kept.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from ..config import Config

SECRET_KEYS = (
    "telegram_bot_token", "anthropic_api_key", "gemini_api_key", "reddit_client_secret",
    "x_bearer_token", "telegram_api_hash", "smtp_password",
)

ENV_FOR = {
    "telegram_bot_token": "JOBRADAR_TELEGRAM_BOT_TOKEN", "telegram_chat_id": "JOBRADAR_TELEGRAM_CHAT_ID",
    "anthropic_api_key": "JOBRADAR_ANTHROPIC_API_KEY", "gemini_api_key": "JOBRADAR_GEMINI_API_KEY",
    "reddit_client_id": "JOBRADAR_REDDIT_CLIENT_ID", "reddit_client_secret": "JOBRADAR_REDDIT_CLIENT_SECRET",
    "x_bearer_token": "JOBRADAR_X_BEARER_TOKEN", "telegram_api_id": "JOBRADAR_TELEGRAM_API_ID",
    "telegram_api_hash": "JOBRADAR_TELEGRAM_API_HASH", "smtp_password": "JOBRADAR_SMTP_PASSWORD",
}

SECTIONS: list[tuple[str, list[str]]] = [
    ("Telegram bot (required) — where jobs are sent",
     ["telegram_bot_token", "telegram_chat_id"]),
    ("Sites to search", ["en", "cn"]),
    ("What you want",
     ["keywords", "exclude_keywords", "remote_only", "regions", "unknown_location", "review_expiry_days", "min_salary_usd",
      "require_salary", "max_posting_age_days", "repost_window_days"]),
    ("Schedule", ["auto_run", "run_interval_hours", "max_messages_per_run"]),
    ("Dashboard", ["web_allow_network"]),
    ("AI (optional)", ["use_ai", "ai_provider", "ai_model", "gemini_api_key", "anthropic_api_key"]),
    ("Auto-apply (optional)",
     ["apply_mode", "apply_channels", "applicant_name", "resume_source", "resume_page_size", "resume_max_pages",
      "resume_font_path",
      "resume_pdf_path", "resume_text_path",
      "apply_min_score", "apply_keywords", "apply_exclude_keywords", "apply_daily_cap",
      "apply_delay_seconds", "apply_contact_cooldown_days", "apply_max_age_days",
      "message_language", "apply_template_zh", "apply_email_subject_zh",
      "apply_template", "apply_email_subject"]),
    ("Your Telegram account (auto-apply) — keep the session file private",
     ["telegram_api_id", "telegram_api_hash", "telegram_phone", "telegram_session_path"]),
    ("Email (auto-apply)",
     ["smtp_host", "smtp_port", "smtp_ssl", "smtp_user", "smtp_password", "smtp_from"]),
    ("API tokens for optional sources",
     ["reddit_client_id", "reddit_client_secret", "x_bearer_token"]),
]

_FIELDS = {f.name: f for f in dataclasses.fields(Config)}
_CHOICES = {
    "resume_source": ("generated", "uploaded"),
    "resume_page_size": ("letter", "a4", "auto"),
    "apply_mode": ("off", "dry_run", "auto"),
    "ai_provider": ("gemini", "claude"),
    "unknown_location": ("review", "send", "drop"),
    "message_language": ("zh", "en", "auto"),
}


class ConfigError(ValueError):
    """A value the user entered is invalid (message is shown to them)."""


# ------------------------------------------------------------------ reading
def read_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def mask(value: str) -> str:
    v = str(value)
    return "…" + v[-4:] if len(v) > 8 else "set"


# --------------------------------------------------------------- validation
def coerce(key: str, value: Any) -> Any:
    """Validate one dashboard value against the Config field it targets.
    Returns the value to store, or None to remove the key."""
    if key not in _FIELDS:
        raise ConfigError(f"unknown setting {key!r}")
    kind = str(_FIELDS[key].type).replace(" ", "")
    label = key.replace("_", " ")
    if value is None or (isinstance(value, str) and not value.strip() and "None" in kind):
        return None
    try:
        if kind == "bool":
            if not isinstance(value, bool):
                raise TypeError
            return value
        if kind == "int":
            if isinstance(value, bool):
                raise TypeError
            n = int(str(value).strip())
            if n < 0:
                raise ConfigError(f"{label} can't be negative")
            return n
        if kind == "float":
            n = float(str(value).strip())
            if n < 0 or n != n or n in (float("inf"),):
                raise ConfigError(f"{label} must be a positive number")
            return n
        if kind in ("str", "str|None"):
            s = str(value) if key in ("apply_template", "apply_template_zh") else str(value).strip()
            if key in _CHOICES and s not in _CHOICES[key]:
                raise ConfigError(f"{label} must be one of {', '.join(_CHOICES[key])}")
            if key == "telegram_api_id" and s and not s.isdigit():
                raise ConfigError("Telegram API ID is a number (from my.telegram.org)")
            return s
        if kind == "list[str]":
            if isinstance(value, str):
                value = re.split(r"[\n,]", value)
            items = [str(v).strip() for v in value if str(v).strip()]
            if key == "apply_channels":
                bad = [i for i in items if i not in ("telegram", "email")]
                if bad:
                    raise ConfigError("apply channels can only be telegram and email")
            return list(dict.fromkeys(items))  # de-duplicate, keep order
        if kind == "list[float]":
            nums = [float(v) for v in value]
            if any(n < 0 for n in nums):
                raise ConfigError(f"{label} can't be negative")
            return nums
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{label}: invalid value {value!r}") from e
    raise ConfigError(f"{label} can't be edited here")


def apply_changes(raw: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    if "unknown_location" in changes:
        out.pop("send_unknown_location", None)  # replaced by unknown_location
    for key, value in changes.items():
        v = coerce(key, value)
        if v is None:
            out.pop(key, None)
        else:
            out[key] = v
    if out.get("apply_delay_seconds") and len(out["apply_delay_seconds"]) != 2:
        raise ConfigError("apply delay needs a minimum and a maximum")
    return out


# ------------------------------------------------------------------ writing
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _str(k)


def _str(s: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes; DEL is the
    # one control character JSON leaves raw that TOML forbids.
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ConfigError("numbers must be finite")
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, list):
        items = [_value(x) for x in v]
        one_line = "[" + ", ".join(items) + "]"
        if len(one_line) <= 88:
            return one_line
        return "[\n" + "".join(f"  {i},\n" for i in items) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    if hasattr(v, "isoformat"):  # TOML dates/times survive a round trip
        return v.isoformat()
    raise ConfigError(f"can't write a value of type {type(v).__name__}")


def render(raw: dict[str, Any]) -> str:
    lines = [
        "# Job Radar configuration — managed by the dashboard (`jobradar web`).",
        "# You can still edit it by hand; the dashboard keeps unknown keys and",
        "# [[sources]] blocks, but not comments. Keep this file private: it",
        "# contains your tokens.",
    ]
    tables = {k: v for k, v in raw.items()
              if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
    plain = {k: v for k, v in raw.items() if k not in tables}
    done: set[str] = set()
    for title, keys in SECTIONS:
        present = [k for k in keys if k in plain]
        if not present:
            continue
        lines += ["", f"# --- {title} " + "-" * max(3, 70 - len(title))]
        lines += [f"{_key(k)} = {_value(plain[k])}" for k in present]
        done.update(present)
    rest = [k for k in plain if k not in done]
    if rest:
        lines += ["", "# --- Other settings " + "-" * 52]
        lines += [f"{_key(k)} = {_value(plain[k])}" for k in rest]
    for name, rows in tables.items():
        for row in rows:
            lines += ["", f"[[{_key(name)}]]"]
            lines += [f"{_key(k)} = {_value(v)}" for k, v in row.items()]
    return "\n".join(lines) + "\n"


def write(path: Path, raw: dict[str, Any]) -> None:
    text = render(raw)
    if tomllib.loads(text) != raw:  # never write something that reads back differently
        raise ConfigError("internal error: the config would not round-trip; nothing was saved")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if os.name != "nt":
            os.chmod(tmp, 0o600)  # it holds tokens
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
