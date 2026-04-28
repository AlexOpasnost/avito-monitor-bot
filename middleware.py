"""Aiogram middlewares — currently per-user throttle.

The bot has no inbound rate limiting otherwise: a single Telegram user
can spam any command (/start, /list, /admin, paste-URL) at the rate
aiogram polls (~10/sec), each call hitting the DB. Five concurrent
slow ops × 30s timeout will exhaust the asyncpg pool (max_size=5)
and stall the bot for every other user.

This middleware silently drops events that arrive faster than the
configured rate per Telegram user. Silent drop matters: replying to
the user with "you're rate-limited" would itself queue an outbound
Telegram call — defeating the protection. The dropped events are
gone; the user simply notices their click didn't register and slows
down naturally.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)


class PerUserThrottle(BaseMiddleware):
    """Drop events arriving faster than `rate_seconds` per Telegram user.

    Memory-bounded: at most ~_BUCKET_MAX users tracked at once;
    over-cap calls evict entries idle for ≥ rate × 8.
    """

    _BUCKET_MAX = 2000

    def __init__(self, rate_seconds: float, label: str = "throttle"):
        self.rate = float(rate_seconds)
        self.label = label
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        uid = getattr(user, "id", None) if user else None
        if uid is None:
            # Service messages, channel posts — no per-user gating.
            return await handler(event, data)

        now = time.monotonic()
        last = self._last.get(uid, 0.0)
        if now - last < self.rate:
            # Silent drop — see module docstring.
            logger.debug(
                "[throttle:%s] drop uid=%d (%.2fs since last)",
                self.label, uid, now - last,
            )
            return None

        self._last[uid] = now

        if len(self._last) > self._BUCKET_MAX:
            cutoff = now - self.rate * 8
            for u in [k for k, t in self._last.items() if t < cutoff]:
                del self._last[u]

        return await handler(event, data)
