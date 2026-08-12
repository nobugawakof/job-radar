"""Optional AI extraction (Claude or Google Gemini).

Off by default. When ``use_ai = true`` and a key for the chosen provider is
present, each posting about to be sent is enriched into cleaner structured
fields — most usefully, the separate **responsibilities** and **requirements**
lists that rule-based parsing can't reliably split out of free-form text.

Two providers are supported, selected by ``ai_provider`` in the config:

* ``"claude"`` — Anthropic Messages API. Paid (needs credits).
* ``"gemini"`` — Google Gemini API. Has a **free tier** (get a key at
  https://aistudio.google.com/apikey), which is plenty for a personal bot.

To keep the project dependency-free, both call their HTTP API directly over
stdlib ``urllib`` — no SDK. Any failure (no key, network error, refusal, bad
JSON) returns ``None`` and the caller falls back to the rule-based fields, so
the AI path is genuinely optional and the core still runs with zero installs.
Only postings that already passed filtering and dedup are enriched, so the API
is called at most once per delivered posting.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jobradar.ai")

# --- Anthropic (Claude) --------------------------------------------------
_CLAUDE_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# --- Google (Gemini) -----------------------------------------------------
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
_GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
# Cache the model we discovered actually works for a given key, so we don't
# call ListModels on every posting. Keyed by api_key.
_GEMINI_MODEL_CACHE: dict[str, str] = {}

# The structured shape both providers must return. Claude gets it as a JSON
# Schema (below); Gemini gets it enumerated in the prompt plus a JSON response
# mode, which avoids fighting Gemini's slightly different schema dialect.
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

# For Gemini we enumerate the exact keys in the prompt (it returns JSON via
# responseMimeType, without a strict schema).
_GEMINI_FIELDS = (
    "\n\nReturn a JSON object with exactly these keys: "
    "title (string), company (string), role (string), location (string), "
    "is_remote (one of: remote, hybrid, onsite, unknown), is_worldwide (boolean), "
    "salary (string), responsibilities (array of strings), requirements (array of "
    "strings), apply (string)."
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


def _to_enrichment(obj: dict[str, Any]) -> Enrichment | None:
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


def enrich(
    text: str,
    *,
    provider: str = "claude",
    api_key: str,
    model: str = "claude-opus-5",
    max_chars: int = 6000,
    timeout: float = 30.0,
) -> Enrichment | None:
    """Return structured fields for one posting, or None to fall back to rules."""
    if not api_key or not text.strip():
        return None
    prov = (provider or "claude").strip().lower()
    try:
        if prov == "gemini":
            return _enrich_gemini(text, api_key=api_key, model=model,
                                  max_chars=max_chars, timeout=timeout)
        return _enrich_claude(text, api_key=api_key, model=model,
                              max_chars=max_chars, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - AI is best-effort; never break a run
        log.warning("AI enrichment failed; falling back to rules: %s", e)
        return None


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ------------------------------------------------------------------ Claude
def _enrich_claude(text, *, api_key, model, max_chars, timeout) -> Enrichment | None:
    if not model or model.startswith("gemini"):
        model = "claude-opus-5"
    body = {
        "model": model,
        "max_tokens": 2048,
        "system": _SYSTEM,
        # Low effort keeps this cheap and fast; structured output guarantees JSON.
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        "messages": [{"role": "user", "content": text[:max_chars]}],
    }
    payload = _post_json(_CLAUDE_URL, body, {
        "anthropic-version": _ANTHROPIC_VERSION, "x-api-key": api_key,
    }, timeout)
    if payload.get("stop_reason") == "refusal":
        log.info("AI enrichment refused; falling back to rules")
        return None
    raw = None
    for block in payload.get("content", []):
        if block.get("type") == "text":
            raw = block.get("text")
            break
    if not raw:
        return None
    try:
        return _to_enrichment(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


# ------------------------------------------------------------------ Gemini
def _gemini_pick_model(api_key: str, timeout: float) -> str | None:
    """Ask Gemini which models this key can actually use, and pick a good fast
    one that supports generateContent. Model names change between Gemini
    generations (2.5 → 3.x …) and differ by project/region, so discovering the
    name beats hardcoding one that 404s."""
    req = urllib.request.Request(_GEMINI_LIST_URL, method="GET")
    req.add_header("x-goog-api-key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    names: list[str] = []
    for m in data.get("models", []):
        methods = m.get("supportedGenerationMethods") or m.get("supportedActions") or []
        if "generateContent" not in methods:
            continue
        name = (m.get("name") or "").split("/")[-1]
        if name:
            names.append(name)

    def score(n: str) -> tuple:
        # Prefer a plain "flash" model; avoid preview/experimental and the
        # heavier or specialised variants; shorter name breaks ties.
        avoid = ("preview", "exp", "vision", "thinking", "image", "tts",
                 "audio", "live", "-8b", "learnlm", "embedding", "aqa")
        return (0 if "flash" in n else 1, 1 if any(a in n for a in avoid) else 0, len(n))

    names.sort(key=score)
    return names[0] if names else None


def _gemini_generate(model, body, api_key, timeout) -> dict:
    return _post_json(_GEMINI_URL.format(model=model), body, {"x-goog-api-key": api_key}, timeout)


def _enrich_gemini(text, *, api_key, model, max_chars, timeout) -> Enrichment | None:
    # Prefer a model we already discovered works for this key (the configured
    # one may be a stale name that 404s). Otherwise use the configured Gemini
    # model, or the default when it's a Claude id / a label like "Gemini API Key".
    cached = _GEMINI_MODEL_CACHE.get(api_key)
    if cached:
        model = cached
    elif not model or not model.startswith("gemini"):
        model = _GEMINI_DEFAULT_MODEL
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM + _GEMINI_FIELDS}]},
        "contents": [{"role": "user", "parts": [{"text": text[:max_chars]}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    try:
        payload = _gemini_generate(model, body, api_key, timeout)
    except urllib.error.HTTPError as e:
        # 404 = that model name isn't available for this key. Discover one that
        # is, cache it, and retry once. (Google renames models across versions.)
        if e.code != 404:
            raise
        picked = _gemini_pick_model(api_key, timeout)
        if not picked or picked == model:
            log.warning("Gemini model %r not available and no alternative found; "
                        "set ai_model to a valid model from aistudio.google.com", model)
            return None
        _GEMINI_MODEL_CACHE[api_key] = picked
        log.info("Gemini model %r unavailable; using %r instead", model, picked)
        payload = _gemini_generate(picked, body, api_key, timeout)
    # A blocked prompt has no candidates, just promptFeedback.
    if payload.get("promptFeedback", {}).get("blockReason"):
        log.info("Gemini blocked the prompt; falling back to rules")
        return None
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw = next((p.get("text") for p in parts if p.get("text")), None)
    if not raw:
        return None
    try:
        return _to_enrichment(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None
