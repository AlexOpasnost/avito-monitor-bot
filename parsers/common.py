"""Shared utilities for all marketplace parsers:
cloudscraper sessions per host, proxy rotation, image download via proxy."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from urllib.parse import urlparse

import httpx

from config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Host allowlist helpers — primary defense against SSRF
# ---------------------------------------------------------------------------
#
# The naive approach of `re.search(r"avito\.ru/", url)` matches a substring
# anywhere in the URL — including the query string. That meant an attacker
# could paste `http://127.0.0.1/admin?u=https://avito.ru/x` and the bot
# would happily fetch 127.0.0.1 (full SSRF on Railway internal network,
# AWS metadata, Redis, Postgres, etc.). The fix is to extract the hostname
# via urlparse and compare it exactly (or against a tightened pattern that
# is full-anchored at hostname boundaries).
#
# Both helpers below normalize the hostname to lowercase and treat any
# parse error as "not a match" — fail-closed by design.

def _parse_hostname(url: str) -> str:
    """Extract the lowercased hostname from a URL, '' on failure."""
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def host_in_allowlist(url: str, allowed: frozenset[str]) -> bool:
    """True iff the URL's hostname (case-insensitive, exact) is in allowed."""
    host = _parse_hostname(url)
    return bool(host and host in allowed)


def host_matches_pattern(url: str, pattern: re.Pattern[str]) -> bool:
    """True iff the URL's hostname fully matches `pattern`.
    Pattern must be anchored to the whole hostname — fullmatch is used,
    not search, so a substring like `?u=avito.ru` cannot slip through."""
    host = _parse_hostname(url)
    return bool(host and pattern.fullmatch(host))


# TODO(abuse-policy): per-host outbound budget — currently nothing
# stops one user with 5 subs from generating ~7,200 fetches/day on a
# single host. Mostly mitigated by Semaphore(1) globally (all fetches
# serialise) and per-source cooldowns (5–10s), but a user with 5 subs
# all pointing at the same domain can still saturate that domain's
# share. Defer until we hit the abuse case in prod — the capacity
# implications are real but not a security boundary.


# ---------------------------------------------------------------------------
# Body-size cap before orjson.loads — OOM defense
# ---------------------------------------------------------------------------
#
# Marketplace responses we've actually observed:
#   - OLX /api/v1/offers: ~200 KB
#   - Kufar __NEXT_DATA__: ~500 KB
#   - Avito mfe-state: ~300 KB
#   - Vinted /api/v2/catalog/items: ~150 KB
#   - Vinted /items/{id} HTML: ~2.1 MB (largest legit case)
# 20 MB cap leaves ~10× headroom over the largest legit body. Without
# it, a hostile / compromised marketplace endpoint could ship a 200 MB
# response that fully buffers in resp.text/.content before parsing,
# OOM-ing Railway's 512 MB container.
MAX_JSON_BYTES = 20 * 1024 * 1024


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


_SECRET_QS_KEYS = ("proxy_key", "key", "token", "secret", "api_key", "apikey")


def _redact_url(url: str) -> str:
    """Strip secret-looking query-string values from a URL for logging.

    The proxy rotation URL (mobileproxy.space) carries a `proxy_key=…`
    that is the *only* credential needed to take over IP rotation for
    this shop's proxy plan. Logging the full URL leaked it to anyone
    with Railway log access. We log host+path only and replace any
    secret-named query-string value with `***`.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        parts = urlparse(url)
        qs = parse_qsl(parts.query, keep_blank_values=True)
        redacted = [
            (k, "***" if k.lower() in _SECRET_QS_KEYS else v)
            for k, v in qs
        ]
        return urlunparse(parts._replace(query=urlencode(redacted)))
    except Exception:
        return "<unloggable url>"


def _redact_proxy_for_log(proxy_url: str) -> str:
    """Strip userinfo from a proxy URL — log host:port only."""
    if not proxy_url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return f"{p.scheme}://{netloc}" if p.scheme else netloc
    except Exception:
        return "<unloggable proxy>"


async def rotate_ip() -> bool:
    """Call proxy rotation URL (if configured). Logs redacted URL only."""
    rotate_url = config.proxy_rotate_url
    if not rotate_url:
        return False
    logger.info(
        "[proxy] rotate URL: %s (len=%d)",
        _redact_url(rotate_url), len(rotate_url),
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(rotate_url)
            # Don't log the body — mobileproxy.space sometimes echoes
            # the proxy_key back in JSON. Length + status is enough for
            # debugging; if you need the body, attach a debug logger
            # to a one-off env-gated path, never to the prod stream.
            body_len = len(resp.text or "")
            logger.info(
                "[proxy] changeip HTTP %d, body_len=%d, final URL=%s",
                resp.status_code, body_len, _redact_url(str(resp.url)),
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
            # allow_redirects=False + max content size cap. A redirect
            # chain from a hostile/compromised CDN could land on
            # 127.0.0.1 / cloud metadata; size cap (5 MB matches
            # Telegram's photo upload limit) bounds memory pressure
            # from a hostile huge image.
            resp = s.get(
                url, proxies=proxies, timeout=15,
                headers=headers or None, allow_redirects=False,
            )
            if resp.status_code != 200:
                logger.debug("[image] HTTP %d for %s", resp.status_code, url[:80])
                return None
            content = resp.content
            if not content or len(content) < 500:
                return None
            if len(content) > 5 * 1024 * 1024:
                logger.debug(
                    "[image] oversized %d bytes for %s — dropping",
                    len(content), url[:80],
                )
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
