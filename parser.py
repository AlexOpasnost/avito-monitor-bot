"""Top-level marketplace dispatcher.

Old callers imported fetch_search_items / rotate_ip / check_proxy_ip /
AvitoItem / download_image_bytes from this module. All still work — but
the actual per-site logic now lives in parsers/<source>.py.
"""
from __future__ import annotations

import logging

from parsers import SearchItem, detect_source, supported_sources
from parsers.avito import avito_download_image  # noqa: F401 (legacy re-export)
from parsers.common import (
    check_proxy_ip,
    download_image_bytes,
    rotate_ip,
)

logger = logging.getLogger(__name__)

# Back-compat — scheduler.py / handlers.py import AvitoItem. SearchItem is
# the new name; leave the old alias until callers have been updated (done
# in the same commit, but keep the alias for safety).
AvitoItem = SearchItem


async def fetch_search_items(
    url: str, proxy: str | None, max_retries: int = 3,
) -> list[SearchItem] | None:
    """Dispatch to the marketplace parser for this URL."""
    source = detect_source(url)
    if source is None:
        logger.warning(
            "[parser] no source matches URL (supported=%s): %s",
            supported_sources(), url[:80],
        )
        return None
    return await source.fetch(url, proxy, max_retries)


__all__ = [
    "SearchItem",
    "AvitoItem",
    "fetch_search_items",
    "rotate_ip",
    "check_proxy_ip",
    "download_image_bytes",
    "detect_source",
    "supported_sources",
]
