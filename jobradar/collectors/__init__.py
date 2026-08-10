"""Source collectors.

Each source type is a small class implementing the :class:`Collector` protocol
in :mod:`jobradar.collectors.base`. Adding a source means adding one collector
and registering it — nothing in filtering, storage, or delivery changes
(NFR-13). Tier B collectors are isolated so a breaking change in one cannot
affect any other component (NFR-15).
"""

from .base import Collector, CollectorError, FetchContext, HttpClient  # noqa: F401
from .registry import build_collector, register, REGISTRY  # noqa: F401
