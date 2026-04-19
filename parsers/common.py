"""Shared utilities for all marketplace parsers:
cloudscraper sessions per host, proxy rotation, image download via proxy."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time

import httpx

from config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User agents
# ---------------------------------------------------------------------------

MODERN_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def pick_user_agent() -> str:
    return random.choice(MODERN_USER_AGENTS)


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

# Sources that MUST route through the mobile proxy (geo-blocked from
# datacenters / Railway-region IPs). Anything else goes direct from the
# container's outbound IP — mobile proxies are usually Russian and can't
# reach EU marketplaces (OLX, Vinted, Mercari) at all.
_PROXIED_SOURCES: frozenset[str] = frozenset({"avito", "kufar"})


def proxy_for_source(source_name: str | None) -> str | None:
    """Pick the right proxy for this marketplace, or None to go direct.

    Centralizes the "should we proxy this?" decision so handlers, scheduler
    and image-download all make the same choice.
    """
    if not source_name:
        return None
    if source_name not in _PROXIED_SOURCES:
        return None
    if not config.proxy_list:
        return None
    return config.proxy_list[0]


async def rotate_ip() -> bool:
    """Call proxy rotation URL (if configured). Logs full URL + body."""
    rotate_url = config.proxy_rotate_url
    if not rotate_url:
        return False
    logger.info("[proxy] rotate URL: %r (len=%d)", rotate_url, len(rotate_url))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(rotate_url)
            body = (resp.text or "").strip()[:300]
            logger.info(
                "[proxy] changeip HTTP %d, body=%r, final URL=%r",
                resp.status_code, body, str(resp.url),
            )
            return resp.status_code == 200
    except Exception as e:
        logger.warning("rotate_ip failed: %s", e)
        return False


async def check_proxy_ip() -> str | None:
    if not config.proxy_list:
        return None
    try:
        async with httpx.AsyncClient(proxy=config.proxy_list[0], timeout=15) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            if resp.status_code == 200:
                return resp.json().get("ip")
    except Exception as e:
        logger.warning("check_proxy_ip failed: %s", e)
    return None


def proxies_dict(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


# ---------------------------------------------------------------------------
# Cloudscraper session pool — one session per host so each site has its
# own cookies. TTL 300s.
# ---------------------------------------------------------------------------

_session_pool: dict[str, tuple[object, float]] = {}  # host -> (session, created_at)


def get_cloudscraper(host: str, warmup_urls: list[str] | None = None,
                    proxy: str | None = None) -> object:
    """Get-or-create cloudscraper session for the given host, with warmup.
    Each host has its own session so cookies don't leak across sites."""
    global _session_pool
    now = time.time()
    existing = _session_pool.get(host)
    if existing and (now - existing[1]) < 300:
        return existing[0]

    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    ua = pick_user_agent()
    s.headers["User-Agent"] = ua
    logger.info("[%s] session created, UA=%s, id=%s", host, ua, id(s))

    proxies = proxies_dict(proxy)

    # ipify probe — what IP will the target site see?
    try:
        r = s.get("https://api.ipify.org?format=json", proxies=proxies, timeout=15)
        if r.status_code == 200:
            logger.info("[%s] pre-warmup visible IP: %s",
                        host, (r.json() or {}).get("ip", "?"))
    except Exception as e:
        logger.debug("[%s] ipify probe failed: %s", host, str(e)[:120])

    # Warmup — visit home page(s) to collect cookies
    for u in (warmup_urls or []):
        try:
            r = s.get(u, proxies=proxies, timeout=20)
            logger.info("[%s] warmup %s: HTTP %d, %d cookies",
                        host, u, r.status_code, len(s.cookies))
        except Exception as e:
            logger.warning("[%s] warmup %s failed: %s", host, u, str(e)[:120])

    # Cooldown so the target IP isn't hot when the real request fires
    cooldown = random.uniform(5, 7)
    logger.info("[%s] warmup cooldown %.1fs", host, cooldown)
    time.sleep(cooldown)

    _session_pool[host] = (s, now)
    return s


def invalidate_session(host: str) -> None:
    """Drop cached session for host (use after IP rotation)."""
    _session_pool.pop(host, None)


# ---------------------------------------------------------------------------
# Image download (bypasses Telegram's blocked access to most CDNs)
# ---------------------------------------------------------------------------

_IMG_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")


async def download_image_bytes(url: str, host: str = "generic",
                                referer: str | None = None,
                                proxy: str | None = None) -> bytes | None:
    """Download an image via the shared session + proxy. Returns bytes on
    success, None otherwise. Telegram can't reach most marketplace CDNs
    directly; we act as the fetcher.

    If `proxy` is not explicitly given, choose per-source: Russian mobile
    proxy for proxied sources, direct otherwise (OLX/Vinted/Mercari CDNs
    expect EU-reachable IPs)."""
    if not url:
        return None
    actual_proxy = proxy if proxy is not None else proxy_for_source(host)

    def _do():
        try:
            s = get_cloudscraper(host, proxy=actual_proxy)
            proxies = proxies_dict(actual_proxy)
            headers = {}
            if referer:
                headers["Referer"] = referer
            resp = s.get(url, proxies=proxies, timeout=15, headers=headers or None)
            if resp.status_code != 200:
                logger.debug("[image] HTTP %d for %s", resp.status_code, url[:80])
                return None
            content = resp.content
            if not content or len(content) < 500:
                return None
            if content.startswith(_IMG_MAGIC):
                return content
            logger.debug("[image] not an image: %s, first bytes=%r",
                         url[:80], content[:8])
            return None
        except Exception as e:
            logger.debug("[image] download err: %s", e)
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do)


# ---------------------------------------------------------------------------
# Image URL helpers used across sources
# ---------------------------------------------------------------------------

def looks_like_image_url(u: object, extra_hosts: tuple[str, ...] = ()) -> bool:
    if not isinstance(u, str) or not u.startswith("http"):
        return False
    low = u.lower()
    if any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return True
    if extra_hosts and any(h in low for h in extra_hosts):
        return True
    return False


# ---------------------------------------------------------------------------
# Global Avito-lock (legacy) — exported for parser.py compatibility.
# Serializes all marketplace requests process-wide so two subs never fire at
# the same time regardless of who calls fetch_search_items.
# ---------------------------------------------------------------------------

_global_lock = asyncio.Lock()


def global_request_lock() -> asyncio.Lock:
    return _global_lock
