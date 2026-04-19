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
from .olx import OlxSource

SOURCES: list[Source] = [
    AvitoSource(),
    KufarSource(),
    OlxSource(),
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


__all__ = ["SearchItem", "Source", "SOURCES", "detect_source", "supported_sources"]
