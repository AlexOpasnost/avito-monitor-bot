"""Per-subscription async scheduler."""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import orjson

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from aiogram.types import BufferedInputFile

from config import config
from database import db
from parser import (
    SearchItem,
    detect_source,
    download_image_bytes,
    fetch_search_items,
)
from bot_i18n import date_template, default_tz_for_lang, timezone_short
from parsers import is_source_disabled, source_display_name
from parsers.common import proxy_for_source
from parsers.currency import format_with_estimate

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
MAX_ITEMS_PER_CYCLE = 10
MAX_AGE_SECONDS = 2 * 24 * 3600  # 2 days


def _parse_blacklist(raw) -> list[str]:
    """Normalize the `filter_blacklist` value coming back from asyncpg.
    JSONB columns may surface as either a Python list (when a codec
    is registered) or as a JSON-encoded str. Either way we want a
    plain list[str] of lowercased tokens for matching."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = orjson.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    return [w for w in raw if isinstance(w, str) and w]


def _matches_blacklist(item: SearchItem, blacklist: list[str]) -> bool:
    """True if any stop-word appears as a substring in the item's
    title or description (case-insensitive). Substring (not word-
    boundary) matches let `женск` catch the whole declension family
    (`женский / женское / женская`) — saves the user from listing
    every form."""
    haystack = " ".join([
        item.title or "",
        item.description or "",
    ]).lower()
    if not haystack.strip():
        return False
    return any(word in haystack for word in blacklist)


async def _notify_user_sub_deactivated(bot: Bot, sub: dict) -> None:
    """DM the owner of `sub` that their subscription was paused
    automatically after too many parse errors. Without this notice
    users discover by accident days later that notifications stopped
    — the leading churn signal in support threads.

    Best-effort: any exception (TelegramForbidden — user blocked the
    bot, RetryAfter, etc.) is swallowed with a log. We don't loop
    on retry; if the user blocked us, they'll re-subscribe via
    /start when they want service back."""
    tg_id = sub.get("telegram_id")
    if not tg_id:
        return
    try:
        from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    except ImportError:
        TelegramForbiddenError = TelegramRetryAfter = Exception
    try:
        await bot.send_message(
            tg_id,
            "⚠️ <b>Один из твоих поисков поставлен на паузу</b>\n\n"
            "10 проверок подряд закончились ошибкой — обычно это значит, "
            "что маркетплейс изменил формат страницы или ссылка устарела. "
            "Открой <b>📋 Мои поиски</b>, проверь URL — если нужно, "
            "удали и добавь заново.",
            parse_mode="HTML",
        )
    except TelegramForbiddenError:
        # User blocked the bot — pause everything for them so we
        # stop hammering an unreachable chat (per-call overhead is
        # small but the per-cycle log noise is real).
        try:
            from database import db as _db
            user_id_int = sub.get("user_id")
            if user_id_int:
                await _db.deactivate_user_tariff(user_id_int)
        except Exception:
            logger.exception(
                "[scheduler] cleanup-on-block failed for user_id=%r",
                sub.get("user_id"),
            )
    except Exception as e:
        logger.warning(
            "[scheduler] notify-deactivated DM failed for tg=%s: %s",
            tg_id, str(e)[:120],
        )


async def _tariff_renewal_funnel(bot: Bot, stop_event: asyncio.Event):
    """Background task: DM users about expiring / expired paid tariffs.

    Two notices per tariff cycle:
    - 24 h before expiry: "your subscription ends tomorrow, renew?"
    - On expiry: "your subscription has ended, renew to resume."

    Both are gated by `users.expiry_warned_for` / `expired_notified_for`
    matching the current `tariff_expires_at` — so renewals (which bump
    expires_at) reset the gates automatically; re-runs in the same
    window skip already-DM'd users.

    Runs every 30 min. Without this, a paid user vanishes silently at
    expiry — no funnel, no churn signal, no chance to recover the
    revenue. The single highest-leverage UX fix in the audit.
    """
    interval = 30 * 60
    # Short warmup so a fresh boot doesn't slam users right away with
    # a possibly-stale-config DM blast.
    initial_delay = 120
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=initial_delay)
        return
    except asyncio.TimeoutError:
        pass

    try:
        from aiogram.exceptions import TelegramForbiddenError
    except ImportError:
        TelegramForbiddenError = Exception

    while not stop_event.is_set():
        try:
            soon = await db.find_users_for_expiry_warning(hours_before=24)
            for u in soon:
                tg_id = u["telegram_id"]
                user_id = u["id"]
                exp = u["tariff_expires_at"]
                try:
                    await bot.send_message(
                        tg_id,
                        "⏳ <b>Тариф заканчивается завтра</b>\n\n"
                        "Чтобы поиски не остановились, продли подписку — "
                        "👉 /start → 💎 Тарифы.\n\n"
                        f"<i>Заканчивается: {exp.strftime('%d.%m %H:%M UTC')}</i>",
                        parse_mode="HTML",
                    )
                    await db.mark_expiry_warned(user_id, exp)
                except TelegramForbiddenError:
                    # Silent — user blocked the bot; nothing to do.
                    await db.mark_expiry_warned(user_id, exp)
                except Exception as e:
                    logger.warning(
                        "[funnel] expiry-warning DM failed tg=%s: %s",
                        tg_id, str(e)[:120],
                    )

            done = await db.find_users_just_expired(lookback_hours=6)
            for u in done:
                tg_id = u["telegram_id"]
                user_id = u["id"]
                exp = u["tariff_expires_at"]
                try:
                    await bot.send_message(
                        tg_id,
                        "🔚 <b>Тариф закончился</b>\n\n"
                        "Поиски сохранены, но новые объявления не приходят. "
                        "Чтобы возобновить — продли подписку через "
                        "/start → 💎 Тарифы.",
                        parse_mode="HTML",
                    )
                    await db.mark_expired_notified(user_id, exp)
                except TelegramForbiddenError:
                    await db.mark_expired_notified(user_id, exp)
                except Exception as e:
                    logger.warning(
                        "[funnel] expired DM failed tg=%s: %s",
                        tg_id, str(e)[:120],
                    )
            if soon or done:
                logger.info(
                    "[funnel] cycle: %d expiring soon, %d just expired",
                    len(soon), len(done),
                )
        except Exception:
            logger.exception("[funnel] cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass


async def _sent_items_pruner(stop_event: asyncio.Event):
    """Background task: prune sent_items rows older than 30 days every
    24 hours. Bounds DB growth and satisfies 152-ФЗ §5(4) data
    minimization on the per-user shopping-history footprint."""
    interval = 24 * 3600
    # First run after a short warmup so a fresh bot doesn't slam the
    # DB with a multi-million-row DELETE on cold start.
    initial_delay = 600
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=initial_delay)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            deleted = await db.prune_sent_items(days=30)
            if deleted:
                logger.info("[prune] sent_items: deleted %d rows older than 30d", deleted)
        except Exception:
            logger.exception("[prune] sent_items prune failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass


async def run_scheduler(bot: Bot, stop_event: asyncio.Event):
    # Per-source semaphores: previously a single Semaphore(1) for ALL
    # parsers, which meant Avito + OLX + Vinted serialised through one
    # gate and the bot couldn't sustain SLA past ~7 active subs. Each
    # source now gets its own gate. Avito/Kufar/Youla stay at 1 (proxy
    # cookies + anti-bot warmup are per-host stateful); the rest can
    # do 2 in parallel without crossing per-IP rate limits.
    logger.info(
        "Scheduler started (per-sub interval=60s, per-source semaphores, jittered start)"
    )
    sems: dict[str, asyncio.Semaphore] = {
        "avito":        asyncio.Semaphore(1),
        "kufar":        asyncio.Semaphore(1),
        "youla":        asyncio.Semaphore(1),
        "olx":          asyncio.Semaphore(2),
        "vinted":       asyncio.Semaphore(2),
        "mercari":      asyncio.Semaphore(2),
        "fruitsfamily": asyncio.Semaphore(2),
        "grailed":      asyncio.Semaphore(2),
    }
    # Fallback for any source not in the map — keeps unknown future
    # parsers safe-by-default at 1 concurrent fetch.
    fallback_sem = asyncio.Semaphore(1)

    def sem_for(source_name: str) -> asyncio.Semaphore:
        return sems.get(source_name) or fallback_sem

    tasks: dict[int, asyncio.Task] = {}
    pruner_task = asyncio.create_task(_sent_items_pruner(stop_event))
    funnel_task = asyncio.create_task(_tariff_renewal_funnel(bot, stop_event))

    while not stop_event.is_set():
        try:
            subs = await db.get_active_subscriptions()
        except Exception as e:
            logger.error("Failed to load subs: %s", e)
            subs = []

        active_ids = {s["id"] for s in subs}

        # Cancel tasks for removed/finished subscriptions
        for sid in list(tasks):
            if sid not in active_ids or tasks[sid].done():
                if not tasks[sid].done():
                    tasks[sid].cancel()
                tasks.pop(sid, None)

        # Spawn new tasks
        for sub in subs:
            if sub["id"] not in tasks:
                tasks[sub["id"]] = asyncio.create_task(
                    _sub_loop(dict(sub), bot, sem_for, stop_event)
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            pass

    # Shutdown: cancel all subscription tasks
    logger.info("Scheduler stopping, cancelling %d tasks", len(tasks))
    for t in tasks.values():
        t.cancel()
    for t in tasks.values():
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    for bg in (pruner_task, funnel_task):
        bg.cancel()
        try:
            await bg
        except (asyncio.CancelledError, Exception):
            pass
    logger.info("Scheduler stopped")


async def _sub_loop(
    sub: dict, bot: Bot,
    sem_for, stop_event: asyncio.Event,
):
    """One loop per subscription. Resolves the per-source semaphore
    at fetch time (not at spawn) so a sub whose URL is later edited
    to a different source uses the correct gate. Adds startup jitter
    to avoid the thundering-herd-at-restart pattern where N tasks all
    hit the lock at the same minute mark."""
    logger.info("Sub #%d loop started", sub["id"])

    # Startup jitter — uniform 0..30s. Without this, every sub
    # re-synchronises at the same wall-clock minute after a restart
    # and they all queue on the lock together. Distributes the load
    # over the first cycle.
    import random as _random
    try:
        await asyncio.wait_for(
            stop_event.wait(),
            timeout=_random.uniform(0, 30),
        )
        return
    except asyncio.TimeoutError:
        pass

    consecutive_failures = 0

    while not stop_event.is_set():
        try:
            # Always re-read DB-backed fields — never trust the snapshot
            # we got at task spawn time. User may have edited the URL,
            # added stop-words, or paused/renamed the sub since the
            # task started.
            fresh = await db.get_subscription(sub["id"])
            if fresh is None:
                logger.info("Sub #%d deleted/deactivated — exiting loop", sub["id"])
                return
            sub["url"] = fresh["url"]
            sub["last_checked_at"] = fresh["last_checked_at"]
            sub["telegram_id"] = fresh["telegram_id"]
            # Stop-words: edits via the bot's blacklist screen need to
            # take effect on the NEXT cycle, not "after a redeploy".
            # Forgetting this kept the field frozen at task-spawn time
            # and is exactly why a user with 12 stop-words still saw
            # listings with those words appearing.
            sub["filter_blacklist"] = fresh.get("filter_blacklist")

            # Resolve URL+source FIRST so we know which per-source sem
            # to wait on. Detect on the fresh URL — a renamed/edited
            # sub might have moved between sources.
            _url = sub["url"]
            src = detect_source(_url)
            source_name = src.name if src else ""

            async with sem_for(source_name):
                # Compliance kill-switch: skip the cycle entirely if
                # this source is in DISABLED_SOURCES. last_checked_at
                # is updated so the «Last check» moves and the sub
                # doesn't look broken; we explicitly do NOT mark it as
                # a parse failure (that would burn the error budget
                # and pause the sub after 3 cycles).
                if is_source_disabled(source_name):
                    logger.info(
                        "Sub #%d: source=%s is in DISABLED_SOURCES — skipping cycle",
                        sub["id"], source_name,
                    )
                    await db.update_last_checked(sub["id"])
                    consecutive_failures = 0
                    # Sleep until next tick and continue without the
                    # failure-handling block below.
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=60)
                        break
                    except asyncio.TimeoutError:
                        continue
                proxy = proxy_for_source(source_name)
                logger.info(
                    "[scheduler] Sub #%d cycle: source=%s proxy=%s url='%s...%s' (len=%d)",
                    sub["id"], source_name or "?", "mobile" if proxy else "direct",
                    _url[:80], _url[-30:], len(_url),
                )
                items = await fetch_search_items(_url, proxy)

            if items is None:
                consecutive_failures += 1
                logger.warning(
                    "Sub #%d: parse failed (%d in a row)",
                    sub["id"], consecutive_failures,
                )
                # increment_error returns True when the sub crossed
                # the auto-deactivate threshold (10 errors). Until
                # this fix the deactivation was silent — user noticed
                # days later that "notifications stopped". Now we DM
                # them with what happened and how to recover.
                try:
                    deactivated = await db.increment_error(
                        sub["id"], "Parse failed",
                    )
                except Exception:
                    logger.exception(
                        "Sub #%d: increment_error raised — letting next cycle retry",
                        sub["id"],
                    )
                    deactivated = False
                if deactivated:
                    await _notify_user_sub_deactivated(bot, sub)

                if consecutive_failures >= 3:
                    logger.warning(
                        "Sub #%d: 3 consecutive failures — pausing for 10 minutes",
                        sub["id"],
                    )
                    consecutive_failures = 0
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=600)
                        break
                    except asyncio.TimeoutError:
                        continue  # 10 min passed — start fresh cycle
            else:
                consecutive_failures = 0
                await _process_items(sub, items, bot)
                await db.update_last_checked(sub["id"])
                sub["last_checked_at"] = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Sub #%d loop error", sub["id"])
            # Log every failure inside the increment_error call too —
            # previously a bare `except Exception: pass` swallowed
            # secondary DB errors here, meaning the per-sub error
            # counter could stop incrementing while the sub kept
            # failing, never reaching the deactivation threshold.
            try:
                deactivated = await db.increment_error(sub["id"], str(e)[:200])
                if deactivated:
                    await _notify_user_sub_deactivated(bot, sub)
            except Exception:
                logger.exception(
                    "Sub #%d: increment_error itself failed — error counter "
                    "may not advance this cycle", sub["id"],
                )

        # Fixed 60s between cycles — user requirement.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Sub #%d loop stopped", sub["id"])


async def _process_items(sub: dict, items: list[SearchItem], bot: Bot):
    is_first_scan = sub.get("last_checked_at") is None

    # Per-subscription stop-words filter. Skipped on first scan so that
    # the initial backlog gets fully marked as "seen" — otherwise, if
    # the user later edits the blacklist, all the previously-filtered
    # items would suddenly burst-deliver as "new". On steady-state
    # cycles we drop matches before they reach the dedup table at all.
    raw_bl = sub.get("filter_blacklist")
    blacklist = _parse_blacklist(raw_bl)
    # Diagnostic — was INFO during the blacklist-stale-snapshot
    # debugging session, now DEBUG to keep production logs sane. The
    # actual "dropped K items" line below stays INFO so operators
    # still see when stop-words actually fire.
    logger.debug(
        "Sub #%d: blacklist parsed=%d raw_type=%s",
        sub["id"], len(blacklist), type(raw_bl).__name__,
    )
    if blacklist and not is_first_scan:
        before = len(items)
        items = [i for i in items if not _matches_blacklist(i, blacklist)]
        dropped = before - len(items)
        if dropped:
            logger.info(
                "Sub #%d: blacklist dropped %d/%d items",
                sub["id"], dropped, before,
            )

    # Group by source so a single batch insert can be made even if a
    # subscription accidentally returns items from more than one site
    # (shouldn't happen, but harmless safety).
    if is_first_scan:
        by_source: dict[str, list[str]] = {}
        for i in items:
            if i.external_id:
                by_source.setdefault(i.source, []).append(i.external_id)
        for src, ids in by_source.items():
            await db.mark_items_sent_batch(sub["id"], ids, source=src)
        total = sum(len(v) for v in by_source.values())
        logger.info("Sub #%d first scan: marked %d items as seen", sub["id"], total)
        return

    # Find new items via a single batch query — was N round-trips per
    # cycle (50 items × ~30ms = 1.5s extra latency that ate into the
    # 60-s SLA budget). Now one query covers the whole batch via the
    # existing (subscription_id, source, avito_id) composite index.
    candidates = [
        (item.source, item.external_id) for item in items if item.external_id
    ]
    unsent_set = set(await db.filter_unsent_items(sub["id"], candidates))
    new_items: list[SearchItem] = [
        item for item in items
        if item.external_id and (item.source, item.external_id) in unsent_set
    ]

    if not new_items:
        return

    now_ts = int(time.time())
    cutoff = now_ts - MAX_AGE_SECONDS
    fresh = [
        i for i in new_items
        if not i.published_timestamp or i.published_timestamp >= cutoff
    ]

    to_send = fresh[:MAX_ITEMS_PER_CYCLE]

    logger.info(
        "Sub #%d: %d new, %d fresh, %d to send",
        sub["id"], len(new_items), len(fresh), len(to_send),
    )

    # Per-user translation: each subscription belongs to one user, who
    # picked a target language at /start. Translation happens inside
    # _send_notification with a shared cache keyed by (text_hash, lang),
    # so multiple subscriptions in the same language reuse work.
    prefs = await db.get_user_prefs_by_telegram(sub["telegram_id"])

    # Tariff gate — a user whose paid tier has expired keeps their
    # subscriptions on the books but gets nothing pushed to their chat.
    # Items still get marked as seen below so a renewal doesn't flood
    # them with backlog. Admins (config.admin_ids) bypass.
    tg_id = sub["telegram_id"]
    is_admin_user = tg_id in (config.admin_ids or [])
    allow_send = is_admin_user or await db.has_active_tariff(tg_id)
    if not allow_send:
        logger.info(
            "Sub #%d (tg=%d): tariff inactive — skipping %d notifications",
            sub["id"], tg_id, len(to_send),
        )

    # Telegram-specific exception types for cleaner classification.
    # Imported lazily to avoid coupling tests to aiogram internals.
    try:
        from aiogram.exceptions import (
            TelegramForbiddenError,
            TelegramRetryAfter,
        )
    except ImportError:
        TelegramForbiddenError = TelegramRetryAfter = Exception

    sent_keys: set[tuple[str, str]] = set()
    user_blocked_us = False
    if allow_send:
        for item in to_send:
            if user_blocked_us:
                # Once we know the user blocked the bot, stop trying
                # to deliver the rest of this batch — Telegram won't
                # let any of them through, and each attempt logs an
                # error.
                break
            try:
                await _send_notification(bot, sub, item, prefs)
                sent_keys.add((item.source, item.external_id))
                await db.mark_item_sent(sub["id"], item.external_id, source=item.source)
            except TelegramForbiddenError:
                # User blocked the bot. Pause all their subs so we
                # stop hammering an unreachable chat — this is the
                # only path that actually reduces ongoing load.
                logger.info(
                    "[scheduler] tg=%s blocked the bot — pausing their subs",
                    sub.get("telegram_id"),
                )
                user_blocked_us = True
                try:
                    user_id_int = sub.get("user_id")
                    if user_id_int:
                        await db.deactivate_user_tariff(user_id_int)
                except Exception:
                    logger.exception(
                        "[scheduler] cleanup-on-block failed user_id=%r",
                        sub.get("user_id"),
                    )
            except TelegramRetryAfter as e:
                # Rate-limited. Sleep the requested duration; the
                # next item will likely also hit the limit, but
                # we'll have spent the time productively.
                wait = float(getattr(e, "retry_after", 1.0))
                logger.warning(
                    "[scheduler] Telegram rate-limit, sleeping %.1fs", wait,
                )
                await asyncio.sleep(min(wait, 30.0))
            except Exception as e:
                logger.warning(
                    "Send failed for %s/%s: %s",
                    item.source, item.external_id, str(e)[:120],
                )
            await asyncio.sleep(0.5)

    # Mark leftovers as seen so we don't re-process them next cycle
    leftover_by_source: dict[str, list[str]] = {}
    for i in new_items:
        key = (i.source, i.external_id)
        if i.external_id and key not in sent_keys:
            leftover_by_source.setdefault(i.source, []).append(i.external_id)
    for src, ids in leftover_by_source.items():
        await db.mark_items_sent_batch(sub["id"], ids, source=src)


# Sources whose text is NOT already in the user's language by default.
# Avito is RU-native; Kufar (Belarus) ships mixed RU/BE; everything else
# uses the seller's native language. We feed source="auto" to Google so
# it detects each item separately rather than guessing per source.
_TRANSLATED_SOURCES: frozenset[str] = frozenset(
    {"olx", "vinted", "mercari", "avito", "kufar",
     "youla", "fruitsfamily", "grailed"}
)


def _looks_like_lang(text: str, lang: str) -> bool:
    """Per-field heuristic: does `text` look like it's already in
    the script of `lang`? Returns True only when most non-space chars
    fit the expected Unicode block, so listings on Avito written in
    Italian/English (`Pantaloncini corti...`) DON'T match `ru` and
    will get translated.

    Used to skip the Google round-trip per-field, replacing the old
    blanket "Avito is always RU" assumption that left foreign-language
    Avito listings untranslated.
    """
    if not text or not lang:
        return False
    visible = [c for c in text.strip()[:200] if not c.isspace() and not c.isdigit()]
    if not visible:
        return False
    lang = lang.lower()

    def _share(predicate) -> float:
        n = sum(1 for c in visible if predicate(c))
        return n / len(visible)

    if lang in ("ru", "be", "uk", "bg"):
        return _share(lambda c: "Ѐ" <= c <= "ӿ") >= 0.5
    if lang == "ja":
        return _share(lambda c: (
            "぀" <= c <= "ゟ"  # hiragana
            or "゠" <= c <= "ヿ"  # katakana
            or "一" <= c <= "鿿"  # CJK unified
        )) >= 0.5
    if lang == "zh":
        return _share(lambda c: "一" <= c <= "鿿") >= 0.5
    if lang in ("kk",):
        # Kazakh is mostly cyrillic with extra letters in 0x04xx
        return _share(lambda c: "Ѐ" <= c <= "ӿ") >= 0.5
    if lang == "el":
        return _share(lambda c: "Ͱ" <= c <= "Ͽ") >= 0.5
    if lang == "tr":
        return _share(lambda c: c.isascii() or c in "ÇçĞğİıÖöŞşÜü") >= 0.5
    # Latin-script langs (en/de/es/fr/it/pl/pt/nl/ro/cs/hu/sv): ASCII +
    # latin-extended diacritics. Cyrillic share must be near zero.
    cyr_share = _share(lambda c: "Ѐ" <= c <= "ӿ")
    if cyr_share >= 0.2:
        return False
    return _share(lambda c: c.isascii() or "À" <= c <= "ɏ") >= 0.5

# Cap concurrent Google Translate calls so a batch of 10 new items
# doesn't fire 20 parallel requests and get rate-limited into 429s.
_TRANSLATE_SEM = asyncio.Semaphore(4)

# Process-local LRU-ish cache: (text_hash, target_lang) → translated.
# Cleared crudely when it grows past the cap. Different users on the
# same language share entries, which is the whole point of the cache.
_TRANSLATE_CACHE: dict[tuple[int, str], str] = {}
_TRANSLATE_CACHE_MAX = 4000


async def _translate(text: str, target_lang: str) -> str:
    """Translate `text` into `target_lang` (ISO-639-1). Returns the
    original text unchanged on empty input or any library error.

    Result is memoised by (hash(text), target_lang) so different items
    quoting the same condition string ("Nuevo con etiquetas") only call
    Google once per language.
    """
    if not text or not text.strip() or not target_lang:
        return text or ""
    key = (hash(text), target_lang)
    cached = _TRANSLATE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        logger.debug("[translate] deep_translator not installed")
        return text

    def _sync_translate() -> str | None:
        try:
            return GoogleTranslator(source="auto", target=target_lang).translate(
                text[:2500],
            )
        except Exception as e:
            logger.debug("[translate] google err: %s", str(e)[:120])
            return None

    loop = asyncio.get_running_loop()
    async with _TRANSLATE_SEM:
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_translate), timeout=6.0,
            )
        except asyncio.TimeoutError:
            logger.debug("[translate] timeout after 6s")
            return text
        except Exception as e:
            logger.debug("[translate] executor err: %s", e)
            return text
    if not isinstance(result, str) or not result.strip():
        return text
    if len(_TRANSLATE_CACHE) > _TRANSLATE_CACHE_MAX:
        # Crude eviction: drop the oldest half. Order is insertion order
        # in CPython 3.7+, which is good enough for an LRU approximation.
        keys = list(_TRANSLATE_CACHE.keys())[: _TRANSLATE_CACHE_MAX // 2]
        for k in keys:
            _TRANSLATE_CACHE.pop(k, None)
    _TRANSLATE_CACHE[key] = result
    return result


async def _localise_item(
    item: SearchItem, target_lang: str,
) -> tuple[str, str | None, str | None, str | None]:
    """Return (title, description, condition, location) translated into
    target_lang.

    Per-field skip via _looks_like_lang: only fields that are NOT
    already in the target language go to Google. Replaces the old
    blanket "Avito is RU-native, skip everything for ru users" rule
    which left foreign-language Avito listings untranslated.

    The original `item` is left unchanged so the same item can be sent
    to multiple users in different languages from the same scheduler
    cycle without cross-talk."""
    if item.source not in _TRANSLATED_SOURCES:
        return item.title, item.description, item.condition, item.location

    pending: list[tuple[str, str]] = []
    for name, value in (
        ("title", item.title),
        ("desc", item.description),
        ("cond", item.condition),
        ("location", item.location),
    ):
        if not value:
            continue
        if _looks_like_lang(value, target_lang):
            continue
        pending.append((name, value))

    if not pending:
        return item.title, item.description, item.condition, item.location

    try:
        results = await asyncio.gather(
            *(_translate(v, target_lang) for _, v in pending),
            return_exceptions=True,
        )
    except Exception as e:
        logger.debug("[translate] gather err: %s", e)
        return item.title, item.description, item.condition, item.location

    out = {
        "title": item.title,
        "desc": item.description,
        "cond": item.condition,
        "location": item.location,
    }
    for (name, _), r in zip(pending, results):
        if isinstance(r, str) and r.strip():
            out[name] = r
    return out["title"], out["desc"], out["cond"], out["location"]


def _source_button_text(source: str | None) -> str:
    if not source:
        return "🔗 Открыть объявление"
    return f"🔗 Открыть на {source_display_name(source)}"

# Per-source Referer for image downloads — Avito's CDN refuses requests
# without the Avito Referer; other sites have similar checks.
_SOURCE_IMAGE_REFERER = {
    "avito":        "https://www.avito.ru/",
    "kufar":        "https://www.kufar.by/",
    "olx":          "https://www.olx.com/",
    "vinted":       "https://www.vinted.com/",
    "mercari":      "https://jp.mercari.com/",
    "youla":        "https://youla.ru/",
    "fruitsfamily": "https://fruitsfamily.com/",
    "grailed":      "https://www.grailed.com/",
}


async def _send_notification(
    bot: Bot, sub: dict, item: SearchItem, prefs: dict,
):
    # Translate per the receiving user's language preference. This lets
    # one scheduler push the same listing in RU to one user and EN to
    # another without re-fetching it. Translation is cached by
    # (text, lang) so a 10-item batch runs ~10 Google calls, not 30.
    lang = prefs.get("lang") or "ru"
    tz = prefs.get("tz") or default_tz_for_lang(lang)
    title, description, condition, location = await _localise_item(item, lang)
    text = _format_notification(
        item, title=title, description=description, condition=condition,
        location=location,
        user_currency=(prefs.get("currency") or "rub").upper(),
        user_tz=tz, user_lang=lang,
    )
    button_text = _source_button_text(item.source)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, url=item.url)],
    ])

    if item.image_url:
        caption = text if len(text) <= 1024 else text[:1020] + "…"
        # Download via the SAME source-host session that already has
        # warm cookies — passing host=item.source avoids spinning up
        # a fresh "generic" session with its own warmup (~7s) on every
        # image. Per-source Referer placates picky CDNs.
        img_bytes = await download_image_bytes(
            item.image_url,
            host=item.source,
            referer=_SOURCE_IMAGE_REFERER.get(item.source),
            proxy=proxy_for_source(item.source),
        )
        if img_bytes:
            try:
                await bot.send_photo(
                    sub["telegram_id"],
                    photo=BufferedInputFile(img_bytes, filename="photo.jpg"),
                    caption=caption, parse_mode="HTML", reply_markup=keyboard,
                )
                return
            except Exception as e:
                logger.info(
                    "send_photo (bytes) failed (%s) for %s/%s — fallback to text",
                    str(e)[:80], item.source, item.external_id,
                )
        else:
            logger.info(
                "[image] could not download %s for %s/%s",
                item.image_url[:80], item.source, item.external_id,
            )

    await bot.send_message(
        sub["telegram_id"], text,
        parse_mode="HTML", reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def _format_notification(
    item: SearchItem, *,
    title: str | None = None,
    description: str | None = None,
    condition: str | None = None,
    location: str | None = None,
    user_currency: str = "RUB",
    user_tz: str = "Europe/Moscow",
    user_lang: str = "ru",
) -> str:
    """Unified pretty notification format.

    Layout (missing fields collapse their line):
        <title (size, condition)>
        💰 <native price> (~user-currency estimate)
        📍 <location>
        📅 <сегодня/вчера/DD.MM> в HH:MM (МСК)
        <description>                ← short, max ~180 chars
        👤 <seller>

    `title`, `description`, `condition` come from the per-user
    translation step. When omitted (e.g. unit-test path), the item's
    raw fields are used.
    """
    import html as _html

    raw_title = title if title is not None else item.title
    raw_desc = description if description is not None else item.description
    raw_cond = condition if condition is not None else item.condition

    # Decode HTML entities the seller (or scrape path) left in their
    # text — "Jack &amp; Jones" should render as "Jack & Jones".
    raw_title = _html.unescape(raw_title or "")
    if raw_desc:
        raw_desc = _html.unescape(raw_desc)
    if raw_cond:
        raw_cond = _html.unescape(raw_cond)

    # Append (size, condition) in parens after the (translated) title.
    extras: list[str] = []
    if item.size:
        extras.append(item.size)
    if raw_cond:
        extras.append(raw_cond)
    if extras:
        # Avoid duplicate noise if seller already wrote one of these
        title_lc = raw_title.lower()
        unique = [x for x in extras if x.lower() not in title_lc]
        if unique:
            raw_title = f"{raw_title} ({', '.join(unique)})"

    title_html = _escape(raw_title) or "Без названия"
    price_native = _escape(_format_price(item, user_currency)) or "Цена не указана"
    # Location goes through the same translation pass as title/description
    # in _localise_item; fall back to the raw value if translation skipped
    # it (e.g. it was already in target lang).
    raw_loc = location if location is not None else item.location
    location_html = _escape(raw_loc) or "—"
    when = _format_when_local(item.published_timestamp, user_tz, user_lang)

    lines = [
        f"<b>{title_html}</b>",
        f"💰 <b>{price_native}</b>",
        f"📍 {location_html}",
        f"📅 {when}",
    ]

    if raw_desc:
        desc = _prettify_description(raw_desc, _DESC_HARD_MAX)
        if desc:
            # Blank line between header and description — cleaner than a
            # U-bar separator, and leaves room within the 1024-char
            # Telegram photo-caption budget.
            lines.append("")
            lines.append(f"<i>{_escape(desc)}</i>")

    if item.seller_name:
        lines.append(f"\n👤 {_escape(item.seller_name)}")

    return "\n".join(lines)


def _format_price(item: SearchItem, user_currency: str) -> str:
    """Render the price with a user-currency estimate when conversion is
    possible. Falls back to the parser's raw `item.price` string when
    we don't have the numeric value to convert."""
    if item.price_value and item.currency:
        return format_with_estimate(
            item.price_value, item.currency, user_currency,
            fallback=item.price,
        )
    return item.price or "Цена не указана"


def _format_when_local(
    ts: int | None,
    tz_name: str = "Europe/Moscow",
    lang: str = "ru",
) -> str:
    """Pretty-print a unix timestamp in the user's timezone + language.

    Same-day ads → «Today at HH:MM (City)»; previous-day → «Yesterday
    at HH:MM (City)»; older → «DD.MM at HH:MM (City)». Phrasing follows
    `lang` (RU / EN / ES / PL / …), city tag follows `tz_name`.

    Falls back to UTC if the IANA name isn't on the system tzdb (which
    shouldn't happen — `tzdata` package is in requirements.txt — but a
    typo in user input shouldn't crash the renderer).
    """
    if not ts:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"
    dt = datetime.fromtimestamp(ts, tz)
    now = datetime.now(tz)
    hhmm = dt.strftime("%H:%M")
    tz_short = "UTC" if tz is timezone.utc else timezone_short(tz_name, lang)

    if dt.date() == now.date():
        kind = "today"
    elif dt.date() == (now.date() - timedelta(days=1)):
        kind = "yesterday"
    else:
        kind = "date"
    template = date_template(lang, kind)
    return template.format(date=dt.strftime("%d.%m"), time=hhmm, tz=tz_short)


def _escape(s: str) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Compact description — read at a glance, no wall of text in the caption.
# Telegram photo caption limit is 1024; leaving ~240 chars for the
# description keeps titles/prices/seller readable even on longer ads.
_DESC_SOFT_MAX = 160
_DESC_HARD_MAX = 240

# Marketing / boilerplate line-starts to drop. Matched against
# line.lower() with .startswith(). Ordered roughly by frequency.
_FLUFF_PREFIXES = (
    # Polish originals (pre-translation safety net)
    "zapraszamy", "witam", "dzień dobry", "dzien dobry",
    "raty ", "0%", "promocja", "promo ", "negocjuj",
    "sklep stacjonarny", "darmowa dostawa",
    "cena ", "pierwotnie",
    # Russian — after Google-translate these are what the PL/UA/RO
    # boilerplate usually renders as, so the fluff filter still has
    # something to catch post-translation.
    "приглашаем", "добро пожаловать", "здравствуйте", "добрый день",
    "рассрочка", "бесплатная доставка",
    "стационарный магазин", "наш магазин", "наш салон",
    "первоначально", "изначально", "ранее ", "цена снижена",
    "торг ", "торг.", "торг,", "торг!", "договорная",
    # Ukrainian / Romanian / Portuguese / Spanish originals
    "вітаємо", "ласкаво просимо", "знижка",
    "bună ziua", "bine ați venit", "preț ",
    "olá", "bom dia", "seja bem-vindo", "preço",
    "originalmente", "precio original", "antes ",
    "originally", "originally:", "was ", "rrp ",
)


def _is_fluff_line(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return True
    return any(low.startswith(p) for p in _FLUFF_PREFIXES)


def _prettify_description(raw: str, hard_max: int = _DESC_HARD_MAX) -> str:
    """Clean up a seller's free-form description for a Telegram card.

    Strategy:
      - HTML-decode entities (`&amp;` etc.)
      - strip zero-width / NBSP junk + normalize whitespace while
        PRESERVING line breaks (bullet lists stay readable)
      - drop fluff lines from BOTH top and bottom (sellers stick
        "Originally 80€" / "Negotiable" at the end)
      - cut at the last sentence-end period before hard cap; fall back
        to line break, then word boundary
    """
    import html as _html
    import re as _re
    if not raw:
        return ""
    text = _html.unescape(raw)
    # Drop invisible / bidi chars
    for ch in ("\u200B", "\u200C", "\u200D", "\u2060", "\u00AD",
               "\uFEFF", "\u00A0"):
        text = text.replace(ch, " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse repeated spaces/tabs, strip lead/trail whitespace on
    # each line, cap consecutive blank lines.
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r" *\n *", "\n", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    text = _re.sub(r"\.\s*,", ".", text)
    text = _re.sub(r",\s*\.", ".", text)
    text = text.strip()
    if not text:
        return ""

    # Drop fluff lines from BOTH ends. Sellers tend to bracket their
    # actual description with "Hi, welcome" at the top and
    # "originally 80€, negotiable" at the bottom.
    lines = text.split("\n")
    while lines and _is_fluff_line(lines[0]):
        lines.pop(0)
    while lines and _is_fluff_line(lines[-1]):
        lines.pop()
    text = "\n".join(lines).strip()
    if not text:
        return ""

    soft_limit = min(_DESC_SOFT_MAX, hard_max)
    if len(text) <= soft_limit:
        return text

    # Prefer the latest *sentence-end* boundary before the hard cap —
    # `\n\n` before a fluff trailer used to win over a clean period
    # earlier in the text, leaving an awkward "Originally:..." dangling.
    window = text[:hard_max]
    sentence_ends = [
        window.rfind(m) for m in (". ", "! ", "? ", ".\n", "!\n", "?\n")
    ]
    sentence_ends.append(window.rfind("."))  # text ending with a period
    viable_sent = [b for b in sentence_ends if b >= soft_limit // 2]
    if viable_sent:
        cut_at = max(viable_sent) + 1  # include the terminator itself
        return text[:cut_at].rstrip()

    # No sentence end in range — fall back to a line break.
    line_breaks = [window.rfind(m) for m in ("\n\n", "\n")]
    viable_line = [b for b in line_breaks if b >= soft_limit // 2]
    if viable_line:
        out = text[: max(viable_line)].rstrip()
        if not out.endswith((".", "!", "?", "…")):
            out += "…"
        return out

    # No natural boundary — word trim with «…»
    cut = window
    sp = cut.rfind(" ")
    if sp > hard_max // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


