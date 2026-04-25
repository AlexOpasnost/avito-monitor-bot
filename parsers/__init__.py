"""Marketplace parser registry.

Add a new marketplace:
  1. Create parsers/<name>.py with a class implementing base.Source
  2. Import the class below and append its instance to SOURCES
  3. detect_source(url) will then route URLs matching that source.
"""
from __future__ import annotations

from .avito import AvitoSource
from .base import SearchItem, Source
from .kufar import KufarSource
from .mercari import MercariSource
from .olx import OlxSource
from .vinted import VintedSource

SOURCES: list[Source] = [
    AvitoSource(),
    KufarSource(),
    OlxSource(),
    MercariSource(),
    VintedSource(),
]


def detect_source(url: str) -> Source | None:
    """Return the Source that handles this URL, or None."""
    if not url:
        return None
    for s in SOURCES:
        if s.matches(url):
            return s
    return None


def supported_sources() -> list[str]:
    return [s.name for s in SOURCES]


# Single source of truth for human-facing source names.
# Used in user-facing bot messages and inline buttons.
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "avito":   "Авито",
    "kufar":   "Kufar",
    "olx":     "OLX",
    "vinted":  "Vinted",
    "mercari": "Mercari",
    "goofish": "Goofish",
}


def source_display_name(name: str | None) -> str:
    if not name:
        return "маркетплейс"
    return SOURCE_DISPLAY_NAMES.get(name.lower(), name.title())


__all__ = [
    "SearchItem", "Source", "SOURCES",
    "detect_source", "supported_sources",
    "SOURCE_DISPLAY_NAMES", "source_display_name",
]
