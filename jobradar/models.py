"""In-memory domain objects passed between pipeline stages.

Plain dataclasses; the only persistence is the JSON state file
(:mod:`jobradar.state`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .extraction import Salary


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class RawItem:
    """A raw fetched item, before classification/extraction."""

    source: str
    external_id: str
    raw_text: str
    url: str | None = None
    raw_json: str | None = None
    posted_at: datetime | None = None
    title_hint: str | None = None
    location_hint: str | None = None


@dataclass
class Posting:
    """A detected job posting ready to be filtered and sent."""

    title: str
    description: str
    source: str
    source_tier: str
    source_url: str
    posted_at: datetime
    collected_at: datetime
    id: str = field(default_factory=new_id)
    contact: str | None = None
    location: str | None = None
    is_remote: str = "unknown"
    is_worldwide: bool = False
    salary: Salary = field(default_factory=Salary)
    apply_url: str | None = None
    content_hash: str = ""
    # All sources this same job was seen on (cross-source merge, shown in the
    # Telegram message).
    origins: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    # Optional AI-extracted detail (empty unless use_ai is on).
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
