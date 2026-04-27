import asyncio
import asyncpg
import logging
from datetime import datetime, timedelta, timezone

from config import config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        # Step 1: wake up PG (Neon/Railway) with direct connection
        for attempt in range(10):
            try:
                conn = await asyncpg.connect(config.database_url, timeout=30)
                await conn.fetchval("SELECT 1")
                logger.info("DB is awake (attempt %d)", attempt + 1)
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='users')"
                )
                if not exists:
                    logger.info("Creating tables...")
                    await self._create_tables_on_conn(conn)
                else:
                    logger.info("Tables already exist")
                await self._run_migrations(conn)
                await conn.close()
                break
            except Exception as e:
                logger.warning("DB wake attempt %d/10: %s", attempt + 1, e)
                await asyncio.sleep(5)
        else:
            raise RuntimeError("Failed to connect to DB after 10 attempts")

        self.pool = await self._create_pool()
        logger.info("Database pool created")

    async def _create_pool(self) -> asyncpg.Pool:
        return await asyncpg.create_pool(
            config.database_url,
            min_size=0,
            max_size=5,
            command_timeout=120,
            timeout=60,
            max_inactive_connection_lifetime=30,
        )

    async def _execute(self, coro_fn):
        """Execute a DB operation with automatic reconnect on failure."""
        for attempt in range(3):
            conn = None
            try:
                conn = await asyncio.wait_for(self.pool.acquire(), timeout=10)
                return await asyncio.wait_for(coro_fn(conn), timeout=30)
            except (asyncpg.ConnectionDoesNotExistError,
                    asyncpg.InterfaceError,
                    OSError,
                    asyncio.TimeoutError) as e:
                logger.warning("DB error (attempt %d/3): %s", attempt + 1, e)
                if conn:
                    try:
                        await self.pool.release(conn)
                    except Exception:
                        pass
                    conn = None
                try:
                    await asyncio.wait_for(self.pool.close(), timeout=5)
                except Exception:
                    pass
                self.pool = await self._create_pool()
                await asyncio.sleep(1)
            finally:
                if conn:
                    try:
                        await self.pool.release(conn)
                    except Exception:
                        pass
        raise RuntimeError("DB operation failed after 3 attempts")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def _create_tables_on_conn(self, conn):
        await conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            username TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            deleted BOOLEAN DEFAULT FALSE,
            error_count INT DEFAULT 0,
            last_error TEXT,
            last_checked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            filter_whitelist TEXT
        )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS sent_items (
            id SERIAL PRIMARY KEY,
            subscription_id INT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            avito_id TEXT NOT NULL,
            sent_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(subscription_id, avito_id)
        )""")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_active "
            "ON subscriptions(is_active) WHERE is_active = TRUE"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_items_lookup "
            "ON sent_items(subscription_id, avito_id)"
        )

    async def _run_migrations(self, conn):
        """Safe-to-rerun migrations."""
        try:
            await conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE"
            )
            await conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS filter_whitelist TEXT"
            )
            await conn.execute(
                "ALTER TABLE subscriptions ALTER COLUMN url TYPE TEXT"
            )
            # Multi-marketplace support — each subscription/sent-item is now
            # tagged with its source ("avito", "kufar", "olx", ...).
            await conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'avito'"
            )
            await conn.execute(
                "ALTER TABLE sent_items ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'avito'"
            )
            # User notification preferences. `lang` is the deep-translator
            # target language; `currency` is the ISO-4217 code (lowercase
            # by convention) used for the price-conversion hint.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'ru'"
            )
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'rub'"
            )
            # Onboarding flag — flips to TRUE after the user picks both
            # language and currency, so /start re-entries skip the wizard.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded BOOLEAN DEFAULT FALSE"
            )
            # IANA timezone name (e.g. "Europe/Moscow"). NULL means
            # "use the language-derived default" — resolved at read
            # time so we don't bake in wrong defaults for existing rows.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone TEXT"
            )
            # Optional user-given label for a subscription. NULL means
            # "show the source name as default". Renamed via the ✏️
            # button on the «Мои поиски» screen.
            await conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS name TEXT"
            )
            # Customer email — required by YooKassa to issue a fiscal
            # receipt under Мой налог (самозанятый). Asked once per
            # user on first paid purchase, reused on later buys.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT"
            )
            # Paid-tariff state. NULL tariff = user hasn't activated
            # anything (no trial, no purchase). Resolved at read time
            # against the in-code tariff table to know max_subs etc.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS tariff TEXT"
            )
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS tariff_expires_at TIMESTAMPTZ"
            )
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_used "
                "BOOLEAN DEFAULT FALSE"
            )
            # Grandfather everyone who registered before paywall existed —
            # if they ever had a subscription, give them tariff='legacy'
            # (no expiry, max 5 searches) so the new limit doesn't lock
            # them out retroactively.
            await self._apply_once(conn, "grandfather_legacy_users",
                                    self._grandfather_legacy_users)
            # Idempotency log for Telegram Payments. Each successful_payment
            # carries a unique telegram_payment_charge_id; we INSERT-OR-
            # IGNORE before activating the tariff so a double-delivery
            # (Telegram retries on transient bot errors) doesn't double
            # the user's expiry.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    telegram_charge_id TEXT PRIMARY KEY,
                    provider_charge_id TEXT,
                    user_id BIGINT NOT NULL,
                    tariff_id TEXT NOT NULL,
                    amount_minor BIGINT NOT NULL,
                    currency TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Backfill any NULLs that may have crept in from older rows.
            await conn.execute(
                "UPDATE subscriptions SET source='avito' WHERE source IS NULL"
            )
            await conn.execute(
                "UPDATE sent_items SET source='avito' WHERE source IS NULL"
            )
            # Per-(subscription, source, external_id) uniqueness so the same
            # listing ID across different sources doesn't collide.
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sent_items_sub_source_extid
                ON sent_items(subscription_id, source, avito_id)
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_items_source ON sent_items(source)"
            )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await self._apply_once(conn, "cleanup_2026_04_19",
                                    self._cleanup_subscriptions_2026_04_19)
            logger.info("Migrations applied")
        except Exception as e:
            logger.debug("Migration note: %s", e)

    async def _apply_once(self, conn, name: str, op):
        """Run a one-shot migration exactly once, tracked in _migrations."""
        already = await conn.fetchval(
            "SELECT 1 FROM _migrations WHERE name = $1", name,
        )
        if already:
            return
        logger.info("Applying one-shot migration: %s", name)
        await op(conn)
        await conn.execute(
            "INSERT INTO _migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
            name,
        )
        logger.info("One-shot migration %s done", name)

    async def _grandfather_legacy_users(self, conn):
        """One-shot: anyone with a historic subscription gets
        tariff='legacy' so the new paywall doesn't suddenly lock them
        out. New users (registered after this migration) start with
        tariff=NULL and must activate Trial or buy a tier."""
        result = await conn.execute(
            "UPDATE users SET tariff = 'legacy' "
            "WHERE id IN (SELECT DISTINCT user_id FROM subscriptions) "
            "AND tariff IS NULL"
        )
        logger.info("grandfather_legacy_users: %s", result)

    async def _cleanup_subscriptions_2026_04_19(self, conn):
        """Wipe every subscription + sent_items row.

        Reason: URLs saved by older bot versions are truncated (invisible
        unicode / VARCHAR column / old regex). Users will re-add their
        subscriptions and the new code saves the full URL."""
        sent_deleted = await conn.fetchval(
            "WITH d AS (DELETE FROM sent_items RETURNING 1) SELECT COUNT(*) FROM d"
        )
        subs_deleted = await conn.fetchval(
            "WITH d AS (DELETE FROM subscriptions RETURNING 1) SELECT COUNT(*) FROM d"
        )
        logger.warning(
            "cleanup_2026_04_19: deleted %s sent_items + %s subscriptions",
            sent_deleted, subs_deleted,
        )

    # --- Users ---

    async def get_or_create_user(self, telegram_id: int, username: str | None = None) -> int:
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1", telegram_id
            )
            if row:
                return row["id"]
            row = await conn.fetchrow(
                "INSERT INTO users (telegram_id, username) VALUES ($1, $2) RETURNING id",
                telegram_id, username,
            )
            return row["id"]
        return await self._execute(_op)

    async def get_user_prefs(self, user_id: int) -> dict:
        """Return {lang, currency, onboarded, tz}. `tz` is the user's
        explicit pick or None — callers resolve None to a language-
        derived default. Defaults applied for old rows where columns
        are NULL after migration."""
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT COALESCE(lang, 'ru') AS lang, "
                "       COALESCE(currency, 'rub') AS currency, "
                "       COALESCE(onboarded, FALSE) AS onboarded, "
                "       timezone AS tz "
                "FROM users WHERE id = $1",
                user_id,
            )
            if not row:
                return {
                    "lang": "ru", "currency": "rub",
                    "onboarded": False, "tz": None,
                }
            return {
                "lang": row["lang"],
                "currency": row["currency"],
                "onboarded": row["onboarded"],
                "tz": row["tz"],
            }
        return await self._execute(_op)

    async def has_active_tariff(self, telegram_id: int) -> bool:
        """True when the user owns either:
          - tariff='legacy' (no expiry, grandfathered before paywall)
          - any other tariff with tariff_expires_at > now

        Returns False for free/expired/missing users. Used by the
        scheduler to gate notifications: a paid Pro user whose 30 days
        ran out shouldn't keep getting free updates on their old subs.
        Admins are checked separately in the scheduler against
        config.admin_ids and bypass this DB lookup.
        """
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT tariff, tariff_expires_at FROM users "
                "WHERE telegram_id = $1",
                telegram_id,
            )
            if not row:
                return False
            tariff = row["tariff"]
            if tariff == "legacy":
                return True
            exp = row["tariff_expires_at"]
            if not tariff or not exp:
                return False
            return exp > datetime.now(timezone.utc)
        return await self._execute(_op)

    async def get_user_prefs_by_telegram(self, telegram_id: int) -> dict:
        """Same as get_user_prefs but keyed by telegram_id — used by the
        scheduler when sending notifications without a cached user_id."""
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT COALESCE(lang, 'ru') AS lang, "
                "       COALESCE(currency, 'rub') AS currency, "
                "       timezone AS tz "
                "FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            if not row:
                return {"lang": "ru", "currency": "rub", "tz": None}
            return {
                "lang": row["lang"],
                "currency": row["currency"],
                "tz": row["tz"],
            }
        return await self._execute(_op)

    async def set_user_lang(self, user_id: int, lang: str):
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET lang = $2 WHERE id = $1", user_id, lang,
            )
        await self._execute(_op)

    async def set_user_currency(self, user_id: int, currency: str):
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET currency = $2 WHERE id = $1", user_id, currency,
            )
        await self._execute(_op)

    async def set_user_timezone(self, user_id: int, tz: str):
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET timezone = $2 WHERE id = $1", user_id, tz,
            )
        await self._execute(_op)

    async def get_user_email(self, user_id: int) -> str | None:
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT email FROM users WHERE id = $1", user_id,
            )
            if not row:
                return None
            email = row["email"]
            return email.strip() if isinstance(email, str) and email.strip() else None
        return await self._execute(_op)

    async def set_user_email(self, user_id: int, email: str):
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET email = $2 WHERE id = $1", user_id, email,
            )
        await self._execute(_op)

    async def set_user_onboarded(self, user_id: int, value: bool = True):
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET onboarded = $2 WHERE id = $1", user_id, value,
            )
        await self._execute(_op)

    # --- Subscriptions ---

    async def add_subscription(self, user_id: int, url: str,
                                source: str = "avito",
                                max_subscriptions: int | None = None) -> int | None:
        """Insert a new subscription if the user is under their limit.

        `max_subscriptions` is the per-tariff cap resolved by the caller.
        Falls back to config.max_subscriptions for legacy callers that
        haven't been updated to look up the tariff yet.

        COUNT + INSERT runs inside a transaction with SELECT FOR UPDATE
        on the user row so two concurrent adds (e.g. user clicks the
        same URL on phone and desktop simultaneously) can't both pass
        the limit check and exceed the cap by 1.
        """
        if max_subscriptions is None:
            max_subscriptions = config.max_subscriptions
        async def _op(conn):
            async with conn.transaction():
                # Lock the user row so concurrent add_subscription
                # calls for the same user serialise on this row. The
                # SELECT FOR UPDATE blocks the second caller until
                # the first commits, then it sees the updated count.
                await conn.fetchval(
                    "SELECT id FROM users WHERE id = $1 FOR UPDATE",
                    user_id,
                )
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM subscriptions "
                    "WHERE user_id = $1 AND is_active = TRUE AND deleted = FALSE",
                    user_id,
                )
                if count >= max_subscriptions:
                    return None
                row = await conn.fetchrow(
                    "INSERT INTO subscriptions (user_id, url, source) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    user_id, url, source,
                )
                return row["id"]
        return await self._execute(_op)

    async def count_active_subs(self, user_id: int) -> int:
        async def _op(conn):
            return await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions "
                "WHERE user_id = $1 AND is_active = TRUE AND deleted = FALSE",
                user_id,
            ) or 0
        return await self._execute(_op)

    async def get_user_tariff(self, user_id: int) -> dict:
        """Returns {tariff, expires_at, trial_used}.

        `tariff` is the row value (None if never activated). The caller
        cross-references it with the in-code tariff table to compute
        max_subs and figure out whether the tariff is still active by
        comparing expires_at with now."""
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT tariff, tariff_expires_at, "
                "       COALESCE(trial_used, FALSE) AS trial_used "
                "FROM users WHERE id = $1", user_id,
            )
            if not row:
                return {"tariff": None, "expires_at": None, "trial_used": False}
            return {
                "tariff": row["tariff"],
                "expires_at": row["tariff_expires_at"],
                "trial_used": row["trial_used"],
            }
        return await self._execute(_op)

    async def activate_tariff(
        self, user_id: int, tariff_id: str, hours: int,
        is_trial: bool = False,
    ) -> datetime | None:
        """Activate or extend a tariff for the user.

        Behaviour:
        - Same tariff still active → new hours stack on top of expiry
          (renewal). User doesn't lose any unused days.
        - Switching tariff while a previous one is still active →
          base = max(now, current_expiry), so an upgrade preserves the
          remaining days the user already paid for.
        - is_trial=True → atomic UPDATE WHERE trial_used=FALSE; if the
          flag was already TRUE (race with another concurrent click),
          returns None instead of double-activating.

        Returns the new expiry timestamp, or None when a Trial activation
        loses the race (caller should treat as «already used»).
        """
        async def _op(conn):
            now = datetime.now(timezone.utc)

            if is_trial:
                # Atomic CAS: UPDATE only if trial wasn't claimed yet.
                # `tariff_expires_at` set to now + hours unconditionally
                # since this branch only runs when trial_used was FALSE.
                new_exp = now + timedelta(hours=hours)
                row = await conn.fetchrow(
                    "UPDATE users SET tariff = 'trial', "
                    "       tariff_expires_at = $2, trial_used = TRUE "
                    "WHERE id = $1 AND COALESCE(trial_used, FALSE) = FALSE "
                    "RETURNING tariff_expires_at",
                    user_id, new_exp,
                )
                return row["tariff_expires_at"] if row else None

            row = await conn.fetchrow(
                "SELECT tariff, tariff_expires_at FROM users WHERE id = $1",
                user_id,
            )
            current_id = row["tariff"] if row else None
            current_exp = row["tariff_expires_at"] if row else None

            # Both renewal AND upgrade should preserve remaining time.
            # The only case where we reset to `now` is when there's no
            # current expiry or it's already in the past.
            if current_exp and current_exp > now:
                base = current_exp
            else:
                base = now
            new_exp = base + timedelta(hours=hours)

            await conn.execute(
                "UPDATE users SET tariff = $2, tariff_expires_at = $3 "
                "WHERE id = $1",
                user_id, tariff_id, new_exp,
            )
            return new_exp
        return await self._execute(_op)

    async def deactivate_user_tariff(self, user_id: int) -> None:
        """Force the user's tariff back to free state. Used by the
        webhook handler when YooKassa reports a refund — the customer
        got their money back, so we revoke access immediately. Trial
        history is preserved (trial_used stays as-is) so they can't
        re-claim the freebie.
        """
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET tariff = NULL, tariff_expires_at = NULL "
                "WHERE id = $1", user_id,
            )
        await self._execute(_op)

    async def get_telegram_id(self, user_id: int) -> int | None:
        """Reverse-lookup the user's Telegram id from our internal id."""
        async def _op(conn):
            return await conn.fetchval(
                "SELECT telegram_id FROM users WHERE id = $1", user_id,
            )
        return await self._execute(_op)

    async def get_revenue_minor_last_n_days(self, days: int = 365) -> int:
        """Sum of all amount_minor in payments within the last N days.
        Used to track approach to the НПД 2.4M ₽/year ceiling."""
        async def _op(conn):
            value = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM payments "
                "WHERE created_at > NOW() - ($1::int || ' days')::interval",
                days,
            )
            return int(value or 0)
        return await self._execute(_op)

    async def get_payment_user_id(self, payment_id: str) -> int | None:
        """Look up which user a YooKassa payment belonged to. Used by
        the refund webhook to find the affected user without trusting
        the metadata in the refund event (which YooKassa doesn't echo)."""
        async def _op(conn):
            return await conn.fetchval(
                "SELECT user_id FROM payments WHERE telegram_charge_id = $1",
                payment_id,
            )
        return await self._execute(_op)

    async def record_payment(
        self, telegram_charge_id: str, provider_charge_id: str | None,
        user_id: int, tariff_id: str,
        amount_minor: int, currency: str,
    ) -> bool:
        """Record a Telegram-Payments charge for idempotency.

        Returns True if this is a new charge (caller should activate the
        tariff), False if the same telegram_charge_id was already
        recorded (Telegram is retrying the delivery; skip activation).
        """
        async def _op(conn):
            row = await conn.fetchrow(
                "INSERT INTO payments "
                "(telegram_charge_id, provider_charge_id, user_id, "
                " tariff_id, amount_minor, currency) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (telegram_charge_id) DO NOTHING "
                "RETURNING telegram_charge_id",
                telegram_charge_id, provider_charge_id, user_id,
                tariff_id, amount_minor, currency,
            )
            return row is not None
        return await self._execute(_op)

    async def get_user_subscriptions(self, user_id: int):
        async def _op(conn):
            return await conn.fetch(
                "SELECT id, url, is_active, created_at, last_checked_at, "
                "       error_count, "
                "       COALESCE(source, 'avito') AS source, name "
                "FROM subscriptions WHERE user_id = $1 AND deleted = FALSE "
                "ORDER BY created_at DESC",
                user_id,
            )
        return await self._execute(_op)

    async def set_subscription_name(
        self, sub_id: int, user_id: int, name: str | None,
    ) -> bool:
        """Rename a subscription. Verifies ownership so a forged
        callback can't relabel another user's row. Returns True on
        success, False if the subscription doesn't belong to the user
        or is already deleted."""
        async def _op(conn):
            row = await conn.fetchrow(
                "UPDATE subscriptions SET name = $3 "
                "WHERE id = $1 AND user_id = $2 AND deleted = FALSE "
                "RETURNING id",
                sub_id, user_id, name,
            )
            return row is not None
        return await self._execute(_op)

    async def get_subscription_owned_by(
        self, sub_id: int, user_id: int,
    ) -> dict | None:
        """Fetch a subscription row only if it belongs to `user_id`.
        Returns None for forged IDs or another user's row."""
        async def _op(conn):
            return await conn.fetchrow(
                "SELECT id, url, COALESCE(source, 'avito') AS source, name "
                "FROM subscriptions "
                "WHERE id = $1 AND user_id = $2 AND deleted = FALSE",
                sub_id, user_id,
            )
        return await self._execute(_op)

    async def get_active_subscriptions(self):
        async def _op(conn):
            return await conn.fetch(
                "SELECT s.id, s.url, s.user_id, s.last_checked_at, "
                "       COALESCE(s.source, 'avito') AS source, u.telegram_id "
                "FROM subscriptions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.is_active = TRUE AND s.deleted = FALSE"
            )
        return await self._execute(_op)

    async def get_subscription(self, sub_id: int):
        """Return a single live subscription row, or None if it was
        deleted/deactivated. Used by the scheduler to re-read URL on
        every cycle so updates from the user are picked up immediately."""
        async def _op(conn):
            return await conn.fetchrow(
                "SELECT s.id, s.url, s.user_id, s.last_checked_at, "
                "       COALESCE(s.source, 'avito') AS source, u.telegram_id "
                "FROM subscriptions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.id = $1 AND s.is_active = TRUE AND s.deleted = FALSE",
                sub_id,
            )
        return await self._execute(_op)

    async def deactivate_subscription(self, sub_id: int):
        async def _op(conn):
            await conn.execute(
                "UPDATE subscriptions SET is_active = FALSE, deleted = TRUE WHERE id = $1",
                sub_id,
            )
        await self._execute(_op)

    async def deactivate_all(self, user_id: int):
        async def _op(conn):
            await conn.execute(
                "UPDATE subscriptions SET is_active = FALSE "
                "WHERE user_id = $1 AND is_active = TRUE AND deleted = FALSE",
                user_id,
            )
        await self._execute(_op)

    async def reactivate_all(self, user_id: int) -> int:
        async def _op(conn):
            result = await conn.execute(
                "UPDATE subscriptions SET is_active = TRUE, error_count = 0, "
                "last_checked_at = NULL "
                "WHERE user_id = $1 AND is_active = FALSE AND deleted = FALSE",
                user_id,
            )
            return int(result.split()[-1])
        return await self._execute(_op)

    async def update_last_checked(self, sub_id: int):
        async def _op(conn):
            await conn.execute(
                "UPDATE subscriptions SET last_checked_at = $2, error_count = 0, last_error = NULL "
                "WHERE id = $1",
                sub_id, datetime.now(timezone.utc),
            )
        await self._execute(_op)

    async def increment_error(self, sub_id: int, error_msg: str) -> bool:
        async def _op(conn):
            row = await conn.fetchrow(
                "UPDATE subscriptions SET error_count = error_count + 1, last_error = $2 "
                "WHERE id = $1 RETURNING error_count",
                sub_id, error_msg,
            )
            if row and row["error_count"] >= config.max_errors_before_deactivate:
                await conn.execute(
                    "UPDATE subscriptions SET is_active = FALSE WHERE id = $1", sub_id
                )
                return True
            return False
        return await self._execute(_op)

    # --- Sent Items ---

    async def is_item_sent(self, sub_id: int, external_id: str,
                            source: str = "avito") -> bool:
        async def _op(conn):
            return await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM sent_items "
                "WHERE subscription_id = $1 AND source = $2 AND avito_id = $3)",
                sub_id, source, external_id,
            )
        return await self._execute(_op)

    async def mark_item_sent(self, sub_id: int, external_id: str,
                              source: str = "avito"):
        async def _op(conn):
            await conn.execute(
                "INSERT INTO sent_items (subscription_id, source, avito_id) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                sub_id, source, external_id,
            )
        await self._execute(_op)

    async def mark_items_sent_batch(self, sub_id: int,
                                     external_ids: list[str],
                                     source: str = "avito"):
        if not external_ids:
            return
        async def _op(conn):
            await conn.executemany(
                "INSERT INTO sent_items (subscription_id, source, avito_id) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                [(sub_id, source, eid) for eid in external_ids if eid],
            )
        await self._execute(_op)

    # --- Profile / Stats ---

    async def get_user_profile(self, user_id: int) -> dict:
        async def _op(conn):
            user = await conn.fetchrow(
                "SELECT telegram_id, username, created_at FROM users WHERE id = $1", user_id
            )
            total_subs = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = $1 AND deleted = FALSE",
                user_id,
            )
            active_subs = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions "
                "WHERE user_id = $1 AND is_active = TRUE AND deleted = FALSE",
                user_id,
            )
            total_found = await conn.fetchval(
                "SELECT COUNT(*) FROM sent_items si "
                "JOIN subscriptions s ON s.id = si.subscription_id "
                "WHERE s.user_id = $1", user_id
            )
            last_found = await conn.fetchrow(
                "SELECT si.avito_id, si.sent_at FROM sent_items si "
                "JOIN subscriptions s ON s.id = si.subscription_id "
                "WHERE s.user_id = $1 ORDER BY si.sent_at DESC LIMIT 1", user_id
            )
            return {
                "user": user,
                "total_subs": total_subs,
                "active_subs": active_subs,
                "total_found": total_found,
                "last_found": last_found,
            }
        return await self._execute(_op)

    async def get_admin_stats(self) -> dict:
        async def _op(conn):
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            active_subs = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions "
                "WHERE is_active = TRUE AND deleted = FALSE"
            )
            unique_urls = await conn.fetchval(
                "SELECT COUNT(DISTINCT url) FROM subscriptions "
                "WHERE is_active = TRUE AND deleted = FALSE"
            )
            total_sent = await conn.fetchval("SELECT COUNT(*) FROM sent_items")
            last_checked = await conn.fetchval(
                "SELECT MAX(last_checked_at) FROM subscriptions"
            )
            new_users_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '24 hours'"
            )
            sent_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM sent_items WHERE sent_at > NOW() - INTERVAL '24 hours'"
            )
            # Revenue snapshot — total + 30-day window. amount_minor is
            # in kopeks/cents, callers convert to display units.
            revenue_total = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM payments"
            )
            revenue_30d = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM payments "
                "WHERE created_at > NOW() - INTERVAL '30 days'"
            )
            paid_users_total = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM payments"
            )
            paid_users_30d = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM payments "
                "WHERE created_at > NOW() - INTERVAL '30 days'"
            )
            # Active paid tariffs right now (not legacy, not free, not expired)
            active_paid_users = await conn.fetchval(
                "SELECT COUNT(*) FROM users "
                "WHERE tariff IN ('basic', 'advanced', 'pro') "
                "  AND tariff_expires_at IS NOT NULL "
                "  AND tariff_expires_at > NOW()"
            )
            return {
                "total_users": total_users,
                "active_subs": active_subs,
                "unique_urls": unique_urls,
                "total_sent": total_sent,
                "last_checked": last_checked,
                "new_users_24h": new_users_24h,
                "sent_24h": sent_24h,
                "revenue_total": revenue_total,
                "revenue_30d": revenue_30d,
                "paid_users_total": paid_users_total,
                "paid_users_30d": paid_users_30d,
                "active_paid_users": active_paid_users,
            }
        return await self._execute(_op)

    async def get_admin_user_list(self, limit: int = 30) -> list[dict]:
        """Latest paid users, newest first. Each row carries enough
        info to render a one-line summary in the admin panel."""
        async def _op(conn):
            rows = await conn.fetch(
                "SELECT u.id, u.telegram_id, u.username, u.tariff, "
                "       u.tariff_expires_at, u.created_at, "
                "       COALESCE((SELECT COUNT(*) FROM subscriptions s "
                "                 WHERE s.user_id = u.id "
                "                   AND s.is_active = TRUE "
                "                   AND s.deleted = FALSE), 0) AS active_subs, "
                "       COALESCE((SELECT SUM(amount_minor) FROM payments p "
                "                 WHERE p.user_id = u.id), 0) AS lifetime_paid "
                "FROM users u "
                "WHERE u.tariff IS NOT NULL "
                "ORDER BY "
                "  CASE WHEN u.tariff IN ('basic','advanced','pro') THEN 0 ELSE 1 END, "
                "  u.tariff_expires_at DESC NULLS LAST, "
                "  u.created_at DESC "
                "LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]
        return await self._execute(_op)

    async def get_admin_user_detail(self, telegram_id: int) -> dict | None:
        """Full drill-down for one telegram_id: profile + subscriptions
        + payment history. Returns None when the user doesn't exist."""
        async def _op(conn):
            user = await conn.fetchrow(
                "SELECT id, telegram_id, username, created_at, "
                "       tariff, tariff_expires_at, trial_used "
                "FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            if not user:
                return None
            subs = await conn.fetch(
                "SELECT id, url, source, name, is_active, last_checked_at, "
                "       error_count, created_at "
                "FROM subscriptions "
                "WHERE user_id = $1 AND deleted = FALSE "
                "ORDER BY created_at DESC",
                user["id"],
            )
            payments = await conn.fetch(
                "SELECT tariff_id, amount_minor, currency, created_at "
                "FROM payments WHERE user_id = $1 "
                "ORDER BY created_at DESC LIMIT 20",
                user["id"],
            )
            return {
                "user": dict(user),
                "subs": [dict(r) for r in subs],
                "payments": [dict(r) for r in payments],
            }
        return await self._execute(_op)


db = Database()
