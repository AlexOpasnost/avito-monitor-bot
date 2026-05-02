"""Top-level marketplace dispatcher. Per-site logic lives in parsers/<source>.py."""
from __future__ import annotations

import logging

from parsers import SearchItem, detect_source, supported_sources
from parsers.common import (
    check_proxy_ip,
    download_image_bytes,
    rotate_ip,
)

logger = logging.getLogger(__name__)


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
    "fetch_search_items",
    "rotate_ip",
    "check_proxy_ip",
    "download_image_bytes",
    "detect_source",
    "supported_sources",
]
