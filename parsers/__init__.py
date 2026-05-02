"""Marketplace parser registry.

Add a new marketplace:
  1. Create parsers/<name>.py with a class implementing base.Source
  2. Import the class below and append its instance to SOURCES
  3. detect_source(url) will then route URLs matching that source.
"""
from __future__ import annotations

from .avito import AvitoSource
from .base import SearchItem, Source
from .fruitsfamily import FruitsfamilySource
from .grailed import GrailedSource
from .kufar import KufarSource
from .mercari import MercariSource
from .olx import OlxSource
from .vinted import VintedSource
from .youla import YoulaSource

SOURCES: list[Source] = [
    AvitoSource(),
    KufarSource(),
    OlxSource(),
    MercariSource(),
    VintedSource(),
    YoulaSource(),
    FruitsfamilySource(),
    GrailedSource(),
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


def is_source_disabled(name: str | None) -> bool:
    """Compliance kill-switch — checked on every scheduler cycle and
    on every new-subscription attempt. Driven by config.disabled_sources
    which is sourced from the DISABLED_SOURCES env var. Lets the
    operator pull a misbehaving / legally-contested source off the
    air with one Railway redeploy, no code change needed."""
    if not name:
        return False
    # Local import — keeps this module dependency-free at top level
    # (config.py imports os/dotenv which we don't want to drag in
    # for trivial readers like the test harness).
    from config import config
    return name.lower() in (config.disabled_sources or [])


# Single source of truth for human-facing source names.
# Used in user-facing bot messages and inline buttons.
SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "avito":        "Авито",
    "kufar":        "Kufar",
    "olx":          "OLX",
    "vinted":       "Vinted",
    "mercari":      "Mercari",
    "youla":        "Юла",
    "fruitsfamily": "Fruitsfamily",
    "grailed":      "Grailed",
}


def source_display_name(name: str | None) -> str:
    if not name:
        return "маркетплейс"
    return SOURCE_DISPLAY_NAMES.get(name.lower(), name.title())


__all__ = [
    "SearchItem", "Source", "SOURCES",
    "detect_source", "supported_sources", "is_source_disabled",
    "SOURCE_DISPLAY_NAMES", "source_display_name",
]
