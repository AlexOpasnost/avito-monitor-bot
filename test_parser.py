"""Smoke test for the Playwright parser. Run: python test_parser.py [URL]

Hits Avito with the singleton browser, prints summary of items found.
By default uses a wide-open URL that should always have results.
"""
import asyncio
import logging
import sys

# Force-disable proxy for local test (we don't have mobile proxies on dev machine)
import os
os.environ.setdefault("PROXY_LIST", "")
os.environ.setdefault("PROXY_ROTATE_URL", "")
os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test")

DEFAULT_URL = "https://www.avito.ru/moskva/telefony?s=104"


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    log.info("Test URL: %s", url[:120])

    # Import after env vars are set so config picks them up
    from parser import fetch_search_items

    log.info("First scrape (curl-cffi chrome120)...")
    items1 = await fetch_search_items(url)
    if items1 is None:
        log.error("FAIL: no items returned")
        return 1
    log.info("Got %d items", len(items1))
    for i, it in enumerate(items1[:3], 1):
        log.info(
            "  [%d] id=%s title=%r price=%r loc=%r img=%s",
            i, it.avito_id, it.title[:50], it.price, it.location,
            "yes" if it.image_url else "no",
        )

    log.info("Second scrape...")
    items2 = await fetch_search_items(url)
    if items2 is None:
        log.error("FAIL: second scrape returned None")
        return 1
    log.info("Got %d items on second scrape", len(items2))

    if len(items1) < 5:
        log.warning("Only %d items — Avito may have blocked us or filter is narrow", len(items1))
    ids1 = {i.avito_id for i in items1}
    ids2 = {i.avito_id for i in items2}
    log.info("ID overlap between scrapes: %d / %d", len(ids1 & ids2), len(ids1))

    with_images = sum(1 for i in items1 if i.image_url)
    log.info("Items with images: %d / %d", with_images, len(items1))

    log.info("PASS")
    return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
