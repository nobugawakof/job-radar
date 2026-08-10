"""Collector interface and shared HTTP client.

A collector fetches raw content from one source type and yields
:class:`~jobradar.models.RawItem` objects. It does *not* classify, extract,
filter, store, or deliver — those are the pipeline's job. This is the single
well-defined interface NFR-13 requires.

The shared :class:`HttpClient` enforces the collection etiquette the SRS
mandates for every outbound request:

* HTTPS only (NFR-9);
* an honest, non-rotating ``User-Agent`` (SR-5);
* a configurable minimum interval between requests to one host, default one
  request every five seconds (SR-6, C-4);
* respect for HTTP ``429`` / ``Retry-After`` (C-4) and a permanent block on a
  ``403`` that names us (surfaced so the pipeline can mark the source blocked,
  NFR-18).

Because the host is intermittent and the budget is zero, the client uses only
the standard library (urllib).
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Protocol

from ..models import RawItem


class CollectorError(Exception):
    """Raised when a source fails. The pipeline catches this per source so one
    failure never halts the run (SR-3)."""


class SourceBlocked(CollectorError):
    """Raised when a source has explicitly blocked us (NFR-18)."""


@dataclass
class FetchContext:
    """State handed to a collector for one fetch."""

    since: datetime | None            # collect content published after this (FR-2)
    now: datetime
    user_agent: str
    request_interval_s: float = 5.0   # SR-6 default
    config: dict[str, Any] = field(default_factory=dict)


class Collector(Protocol):
    """The one interface a new source must implement (NFR-13)."""

    name: str
    type: str
    tier: str

    def fetch(self, ctx: FetchContext) -> Iterator[RawItem]:
        """Yield raw items published since ``ctx.since``. Raise CollectorError
        on failure; never raise anything else."""
        ...


class HttpClient:
    """Polite, honest, stdlib-only HTTP client shared by collectors."""

    # Per-host timestamp of the last request, to honour SR-6 rate limiting.
    _last_request: dict[str, float] = {}

    def __init__(self, user_agent: str, request_interval_s: float = 5.0, timeout: float = 20.0):
        self.user_agent = user_agent
        self.request_interval_s = request_interval_s
        self.timeout = timeout

    def _throttle(self, host: str) -> None:
        last = HttpClient._last_request.get(host)
        if last is not None:
            wait = self.request_interval_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        HttpClient._last_request[host] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        accept: str = "application/json",
        _retry_429: bool = True,
    ) -> tuple[int, bytes]:
        if not url.lower().startswith("https://"):
            # NFR-9: all outbound requests use HTTPS.
            raise CollectorError(f"refusing non-HTTPS URL: {url}")
        host = urllib.parse.urlparse(url).netloc
        self._throttle(host)

        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", self.user_agent)   # SR-5: honest, fixed identity
        req.add_header("Accept", accept)
        for k, v in (headers or {}).items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited — back off once, then give up for this run
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = int(ra) if (ra and str(ra).isdigit()) else 12
                if _retry_429:
                    # Reddit and friends rate-limit bursts of unauthenticated
                    # requests; a short pause usually clears it. Retry exactly
                    # once so one busy source can't stall the whole run.
                    time.sleep(min(wait, 30))
                    return self.get(url, headers=headers, accept=accept, _retry_429=False)
                raise CollectorError(f"429 rate limited (Retry-After={ra})") from e
            # A 403 is treated as a *retryable* failure, not a permanent block:
            # public APIs (e.g. Bluesky) return transient 403s, and one blip
            # should not permanently kill a Tier A source. A real, persistent
            # block surfaces as repeated failures (and Tier B auto-disables).
            raise CollectorError(f"HTTP {e.code} for {url}") from e
        except urllib.error.URLError as e:
            raise CollectorError(f"network error for {url}: {e.reason}") from e
        except TimeoutError as e:
            raise CollectorError(f"timeout for {url}") from e


# urllib.parse is used above; import here to avoid a top-level unused warning
# in linters while keeping the reference local to the module.
import urllib.parse  # noqa: E402
