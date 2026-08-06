"""In-memory domain objects passed between pipeline stages.

These are plain dataclasses; persistence lives in :mod:`jobradar.repos`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .extraction import Salary


# Posting lifecycle statuses (Section 5 `status` enum), tracked per user.
STATUS_NEW = "new"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_DELIVERED = "delivered"
STATUS_APPLIED = "applied"
STATUS_SAVED = "saved"
STATUS_DISMISSED = "dismissed"
STATUS_EXPIRED = "expired"

ALL_STATUSES = {
    STATUS_NEW, STATUS_PENDING_REVIEW, STATUS_DELIVERED, STATUS_APPLIED,
    STATUS_SAVED, STATUS_DISMISSED, STATUS_EXPIRED,
}


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class RawItem:
    """A raw fetched item, before classification/extraction (FR-6)."""

    source: str
    external_id: str
    raw_text: str
    url: str | None = None
    raw_json: str | None = None
    posted_at: datetime | None = None
    # Structured hints the collector already knows, to aid extraction.
    title_hint: str | None = None
    location_hint: str | None = None


@dataclass
class Posting:
    """A collected posting (global; per-user status lives elsewhere)."""

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
    hiring_countries: list[str] = field(default_factory=list)
    is_worldwide: bool = False
    salary: Salary = field(default_factory=Salary)
    apply_url: str | None = None
    content_hash: str = ""
    origins: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
