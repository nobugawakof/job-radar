"""Collector registry.

Maps a source ``type`` string (as it appears in config, SR-1) to a collector
class. New source types register themselves here; the pipeline builds live
collectors from stored source rows without knowing their concrete classes.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Collector, HttpClient


REGISTRY: dict[str, Callable[..., Collector]] = {}


def register(type_: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        REGISTRY[type_] = cls
        return cls

    return deco


def build_collector(
    *, name: str, type_: str, tier: str, config: dict[str, Any], http: HttpClient
) -> Collector:
    if type_ not in REGISTRY:
        raise KeyError(f"unknown source type: {type_!r} (known: {sorted(REGISTRY)})")
    cls = REGISTRY[type_]
    return cls(name=name, tier=tier, config=config, http=http)


# Importing the collector modules populates the registry via @register.
def load_builtin_collectors() -> None:
    from . import bluesky, hackernews, rss, reddit, telegram, scrape, twitter  # noqa: F401


load_builtin_collectors()
