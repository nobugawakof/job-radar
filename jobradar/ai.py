"""Optional AI extraction (Anthropic API).

Off by default. When ``use_ai = true`` and an API key is present, each posting
about to be sent is enriched by Claude into cleaner structured fields — most
usefully, the separate 岗位职责 (responsibilities) and 岗位要求 (requirements)
lists that rule-based parsing can't reliably split out of free-form text.

To keep the rest of the project dependency-free, this calls the Anthropic
Messages API directly over stdlib ``urllib`` rather than pulling in the SDK —
so the AI path is genuinely optional and the core still runs with zero installs.
Any failure (no key, network error, refusal, bad JSON) returns ``None`` and the
caller falls back to the rule-based fields.

The API key comes from the environment (``JOBRADAR_ANTHROPIC_API_KEY``), never
the config file. Only postings that already passed filtering and dedup are
enriched, so the API is called at most once per delivered posting.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jobradar.ai")

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Structured-output schema Claude must fill. Kept flat and all-required so the
# response is deterministic and easy to merge onto a Posting.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "description": "Concise 'Company — Role' headline"},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "location": {"type": "string", "description": "Hiring location, or '' if unclear"},
        "is_remote": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
        "is_worldwide": {"type": "boolean", "description": "true if hire-from-anywhere"},
        "salary": {"type": "string", "description": "Compensation as written, or ''"},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": {"type": "string"}},
        "apply": {"type": "string", "description": "Email, URL, or handle to apply, or ''"},
    },
    "required": [
        "title", "company", "role", "location", "is_remote", "is_worldwide",
        "salary", "responsibilities", "requirements", "apply",
    ],
}

_SYSTEM = (
    "You extract structured fields from a single job posting. Fill only the "
    "schema fields from what the post actually says. Do not invent details: use "
    "'' for unknown strings, empty arrays for unknown lists, is_remote='unknown' "
    "and is_worldwide=false when unclear. Keep responsibilities and requirements "
    "to short bullet phrases in the post's own language."
)


@dataclass
class Enrichment:
    title: str
    company: str
    role: str
    location: str
    is_remote: str
    is_worldwide: bool
    salary: str
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    apply: str = ""


def enrich(
    text: str,
    *,
    api_key: str,
    model: str = "claude-opus-5",
    max_chars: int = 6000,
    timeout: float = 30.0,
) -> Enrichment | None:
    """Return structured fields for one posting, or None to fall back to rules."""
    if not api_key or not text.strip():
        return None
    body = {
        "model": model,
        "max_tokens": 2048,
        "system": _SYSTEM,
        # Low effort keeps this cheap and fast; structured output guarantees JSON.
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        "messages": [{"role": "user", "content": text[:max_chars]}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_API_URL, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("anthropic-version", _ANTHROPIC_VERSION)
    req.add_header("x-api-key", api_key)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 - AI is best-effort; never break a run
        log.warning("AI enrichment request failed; falling back to rules: %s", e)
        return None

    if payload.get("stop_reason") == "refusal":
        log.info("AI enrichment refused; falling back to rules")
        return None

    # With thinking on, JSON is in the text block — find it rather than [0].
    raw = None
    for block in payload.get("content", []):
        if block.get("type") == "text":
            raw = block.get("text")
            break
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    try:
        return Enrichment(
            title=str(obj.get("title") or "").strip(),
            company=str(obj.get("company") or "").strip(),
            role=str(obj.get("role") or "").strip(),
            location=str(obj.get("location") or "").strip(),
            is_remote=str(obj.get("is_remote") or "unknown"),
            is_worldwide=bool(obj.get("is_worldwide")),
            salary=str(obj.get("salary") or "").strip(),
            responsibilities=[str(x) for x in (obj.get("responsibilities") or [])],
            requirements=[str(x) for x in (obj.get("requirements") or [])],
            apply=str(obj.get("apply") or "").strip(),
        )
    except (TypeError, ValueError):
        return None
