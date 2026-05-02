import asyncio
import asyncpg
import logging
import re
from datetime import datetime, timedelta, timezone

import orjson

from config import config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None
        # Serialises pool tear-down + recreation so a thundering herd of
        # coroutines all hitting a transient DB error can't each spawn
        # their own new pool (which would over-saturate Neon/Railway's
        # connection budget and keep cascading).
        self._pool_swap_lock = asyncio.Lock()

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

    async def _execute(self, coro_fn, *, idempotent: bool = True):
        """Execute a DB operation with automatic reconnect on failure.

        `idempotent` controls retry safety. The retry envelope catches
        connection errors and timeouts — but a TimeoutError on a
        non-idempotent statement (INSERT, INCREMENT) can fire AFTER the
        server committed the change, just before the client got the ack.
        Re-running the statement then duplicates the work. So:

        - idempotent=True (default, safe for SELECT, ON CONFLICT
          DO NOTHING, atomic CAS UPDATEs): retry up to 3 times.
        - idempotent=False: try once; on connection/timeout error,
          give up and propagate so the caller can decide
          (e.g. webhook returns 500 and YooKassa retries the whole
          event with full atomic state).
        """
        max_attempts = 3 if idempotent else 1
        for attempt in range(max_attempts):
            conn = None
            try:
                conn = await asyncio.wait_for(self.pool.acquire(), timeout=10)
                return await asyncio.wait_for(coro_fn(conn), timeout=30)
            except (asyncpg.ConnectionDoesNotExistError,
                    asyncpg.InterfaceError,
                    OSError,
                    asyncio.TimeoutError) as e:
                logger.warning(
                    "DB error (attempt %d/%d, idempotent=%s): %s",
                    attempt + 1, max_attempts, idempotent, e,
                )
                if conn:
                    try:
                        await self.pool.release(conn)
                    except Exception:
                        pass
                    conn = None
                # Pool tear-down + recreate is a thundering-herd risk:
                # without serialisation, every concurrent failing op
                # spawns its own new pool, blowing past PG's connection
                # cap. The lock funnels recreation through one path;
                # subsequent waiters re-check whether the pool was
                # already swapped before tearing it down again.
                broken = self.pool
                async with self._pool_swap_lock:
                    if self.pool is broken:
                        try:
                            await asyncio.wait_for(broken.close(), timeout=5)
                        except Exception:
                            pass
                        self.pool = await self._create_pool()
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(1)
            finally:
                if conn:
                    try:
                        await self.pool.release(conn)
                    except Exception:
                        pass
        if idempotent:
            raise RuntimeError("DB operation failed after 3 attempts")
        raise RuntimeError("DB operation failed (non-idempotent, no retry)")

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
            # Per-subscription stop-words. Items whose title or description
            # contains any of these substrings (case-insensitive) get
            # filtered before notification. Stored as a JSONB array of
            # lowercase strings — e.g. ["женский", "детский", "fake"].
            # Empty/missing = no filtering.
            await conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS "
                "filter_blacklist JSONB DEFAULT '[]'::jsonb"
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
            # Tombstones for users who exercised /delete_my_account
            # (152-ФЗ Art. 14 erasure). When a late YooKassa webhook
            # arrives for a deleted user, we must NOT recreate the
            # users row — that would resurrect their PII against the
            # explicit erasure request and silently re-activate a
            # tariff for someone we promised to forget. The webhook
            # checks this table before calling get_or_create_user.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tombstoned_users (
                    telegram_id BIGINT PRIMARY KEY,
                    deleted_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Compliance audit trail (152-ФЗ Art. 18.1) — track every
            # subject-access / erasure request so РКН can be answered
            # under inspection. Append-only; retained ≥1 year.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS data_subject_requests (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    request_type TEXT NOT NULL,
                    requested_at TIMESTAMPTZ DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    outcome JSONB
                )
            """)
            # Admin-action audit (152-ФЗ Art. 18.1 §1.5) — every
            # /admin user view that surfaces customer PII (email,
            # payments) is logged with which admin saw what.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_access_log (
                    id SERIAL PRIMARY KEY,
                    admin_telegram_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    target_telegram_id BIGINT,
                    accessed_at TIMESTAMPTZ DEFAULT NOW(),
                    details JSONB
                )
            """)
            # Consent capture: timestamp + policy version per user.
            # Empty for legacy rows; backfilled when user re-onboards.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "consent_at TIMESTAMPTZ"
            )
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "consent_policy_version TEXT"
            )
            # Renewal-funnel flags: track which "expires soon" / "expired"
            # DMs we've sent so a long-running scheduler doesn't spam
            # the same user with the same notice every cycle. Each
            # field stores the tariff_expires_at timestamp the notice
            # was sent against — so a tariff renewal (which extends
            # tariff_expires_at) makes the old flag stale automatically.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "expiry_warned_for TIMESTAMPTZ"
            )
            await conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "expired_notified_for TIMESTAMPTZ"
            )
            # `_migrations` MUST exist before the first _apply_once
            # call — _apply_once reads/writes this table to track
            # one-shot migrations. Previously placed below the first
            # _apply_once, which crashed on fresh DBs (caught by the
            # silent debug-log; now we re-raise on failure).
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
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
            # Refund idempotency. The webhook handler INSERTs here with
            # ON CONFLICT DO NOTHING; a second delivery of the same
            # refund_id is then a no-op (no second tariff deactivation,
            # no second DM to the user). Without this, a forged or
            # replayed refund.succeeded event could repeatedly DM the
            # victim — Telegram anti-spam catches the bot first.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS refunds (
                    refund_id TEXT PRIMARY KEY,
                    payment_id TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    amount_minor BIGINT NOT NULL,
                    currency TEXT NOT NULL,
                    processed_at TIMESTAMPTZ DEFAULT NOW()
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

            await self._apply_once(conn, "cleanup_2026_04_19",
                                    self._cleanup_subscriptions_2026_04_19)
            logger.info("Migrations applied")
        except Exception:
            # The whole migration block was previously try/except'd at
            # logger.debug — silently swallowing every schema error,
            # including ones that left the DB in a half-migrated state
            # (no _migrations table created, partial column adds, etc.).
            # Re-raise so a broken schema fails the bot at startup
            # instead of running for hours and corrupting data.
            logger.exception("[migrations] schema migration failed — refusing to start")
            raise

    async def _apply_once(self, conn, name: str, op):
        """Run a one-shot migration exactly once, tracked in _migrations.

        Race protection: two bots starting simultaneously (Railway
        redeploy + lingering old container) both pass the `SELECT 1`
        check, both run the destructive op, both INSERT — the
        migration runs TWICE, the second time against post-cleanup
        state. For destructive ops (cleanup_2026_04_19) this can
        wipe live re-added user data. Fix: take a Postgres advisory
        lock keyed off the migration name; recheck under the lock.
        Anyone serializes on this lock; only the first one through
        runs the op."""
        # `hashtext` is a Postgres-internal stable hash → fits in int8
        # which pg_advisory_xact_lock expects.
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"migration:{name}",
            )
            already = await conn.fetchval(
                "SELECT 1 FROM _migrations WHERE name = $1", name,
            )
            if already:
                return
            logger.info("Applying one-shot migration: %s", name)
            await op(conn)
            await conn.execute(
                "INSERT INTO _migrations (name) VALUES ($1) "
                "ON CONFLICT DO NOTHING",
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
        subscriptions and the new code saves the full URL.

        Sunset guard: after the cutoff date this op refuses to run
        regardless of `_migrations` state. If an operator accidentally
        deletes the `_migrations` row while debugging, this prevents
        a second run from wiping live data months later. The op is
        already idempotent in `_apply_once` — this is belt-and-suspenders
        for the worst-case "operator forgot it was destructive"
        scenario."""
        from datetime import datetime, timezone
        # Cutoff: ~2 weeks past the original migration date. Anyone
        # restoring state past this point should review the code, not
        # blindly let it re-run.
        sunset = datetime(2026, 5, 5, tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > sunset:
            logger.warning(
                "cleanup_2026_04_19: refusing to run past sunset %s — "
                "delete this method or extend the date if you really "
                "need to wipe live data",
                sunset.isoformat(),
            )
            return
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

    async def set_user_onboarded(
        self, user_id: int, value: bool = True,
        policy_version: str | None = None,
    ):
        """Mark onboarding complete. When `value=True` and a
        `policy_version` is supplied, also stamp consent_at = NOW() and
        consent_policy_version = <version>. This is the 152-ФЗ Art. 9
        consent timestamp the operator can show to РКН during an audit.

        For back-compat callers that don't pass policy_version, only
        `onboarded` flips — consent fields remain whatever they were
        (NULL for users who pre-date this change)."""
        async def _op(conn):
            if value and policy_version:
                await conn.execute(
                    "UPDATE users SET onboarded = $2, "
                    "  consent_at = COALESCE(consent_at, NOW()), "
                    "  consent_policy_version = "
                    "    COALESCE(consent_policy_version, $3) "
                    "WHERE id = $1",
                    user_id, value, policy_version,
                )
            else:
                await conn.execute(
                    "UPDATE users SET onboarded = $2 WHERE id = $1",
                    user_id, value,
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
        # Non-idempotent: a TimeoutError after a committed INSERT would
        # otherwise re-run the INSERT and create a duplicate sub. Caller
        # gets the exception and surfaces "ошибка, попробуй ещё раз".
        return await self._execute(_op, idempotent=False)

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

    async def record_refund(
        self, *, refund_id: str, payment_id: str,
        user_id: int, amount_minor: int, currency: str,
    ) -> bool:
        """Record a verified refund for idempotency.

        Returns True if this is the first time we see this refund_id;
        the caller should then deactivate the user's tariff and DM
        them once. Returns False on a duplicate delivery (replay,
        YooKassa retry, our own retry-on-network-error) — caller acks
        and moves on without re-deactivating or re-DMing.

        DEPRECATED for the webhook path — use
        `record_and_deactivate_refund` instead, which folds the
        record + deactivate into a single transaction. The split-
        step variant left a window where the refund was logged but
        the user kept their tariff (deactivate_user_tariff failed
        between the two steps).
        """
        async def _op(conn):
            row = await conn.fetchrow(
                "INSERT INTO refunds "
                "(refund_id, payment_id, user_id, amount_minor, currency) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (refund_id) DO NOTHING "
                "RETURNING refund_id",
                refund_id, payment_id, user_id, amount_minor, currency,
            )
            return row is not None
        return await self._execute(_op)

    async def record_and_deactivate_refund(
        self, *, refund_id: str, payment_id: str,
        user_id: int, amount_minor: int, currency: str,
    ) -> bool:
        """Atomically record a verified refund AND deactivate the
        user's tariff (+ pause active subs).

        Returns True iff this is the first time we see this refund_id
        AND the deactivation actually applied. Caller (webhook) uses
        this to gate the user-DM ("refund processed").

        Why atomic: previously `record_refund` then a separate
        `deactivate_user_tariff` call left a window — if the second
        call failed (transient DB error, race), the refund row was
        already in the table, so a webhook redelivery returned
        is_new=False and the deactivation never re-ran. The customer
        got their money back AND kept the service indefinitely.

        Failure semantics: if deactivation raises, the entire txn
        rolls back, refund row is NOT recorded, webhook returns 500
        and YooKassa retries later. Idempotency-on-success: a second
        delivery sees the refund row, returns False, caller skips
        DM (no spam) but doesn't try to re-deactivate (already done).
        """
        async def _op(conn):
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO refunds "
                    "(refund_id, payment_id, user_id, amount_minor, currency) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (refund_id) DO NOTHING "
                    "RETURNING refund_id",
                    refund_id, payment_id, user_id, amount_minor, currency,
                )
                if row is None:
                    # Duplicate — deactivation already happened in a
                    # prior call (or this user_id was -1, anonymized).
                    return False
                # Same SQL as deactivate_user_tariff but inline so the
                # whole sequence is one transaction. Idempotent if
                # somehow re-run on the same user (UPDATE to NULL is
                # safe).
                await conn.execute(
                    "UPDATE users SET tariff = NULL, "
                    "tariff_expires_at = NULL WHERE id = $1",
                    user_id,
                )
                await conn.execute(
                    "UPDATE subscriptions SET is_active = FALSE "
                    "WHERE user_id = $1 AND is_active = TRUE "
                    "AND deleted = FALSE",
                    user_id,
                )
                return True
        return await self._execute(_op, idempotent=False)

    async def find_users_for_expiry_warning(
        self, hours_before: int = 24,
    ) -> list[dict]:
        """Users whose paid tariff expires within `hours_before` and
        whom we haven't already warned for THIS expiry timestamp.

        The `expiry_warned_for` column stores the exact tariff_expires_at
        we already DM'd against; comparing to the current value means
        renewals (which bump expires_at) automatically reset the flag,
        and re-runs of the cron skip already-DM'd users."""
        async def _op(conn):
            rows = await conn.fetch(
                "SELECT id, telegram_id, tariff, tariff_expires_at "
                "FROM users "
                "WHERE tariff IS NOT NULL "
                "  AND tariff NOT IN ('legacy', 'admin') "
                "  AND tariff_expires_at IS NOT NULL "
                "  AND tariff_expires_at > NOW() "
                "  AND tariff_expires_at <= NOW() + ($1::int || ' hours')::interval "
                "  AND (expiry_warned_for IS NULL "
                "       OR expiry_warned_for <> tariff_expires_at)",
                hours_before,
            )
            return [dict(r) for r in rows]
        return await self._execute(_op)

    async def find_users_just_expired(
        self, lookback_hours: int = 6,
    ) -> list[dict]:
        """Users whose tariff expired within the last `lookback_hours`
        and whom we haven't notified yet for THIS expiry timestamp."""
        async def _op(conn):
            rows = await conn.fetch(
                "SELECT id, telegram_id, tariff, tariff_expires_at "
                "FROM users "
                "WHERE tariff IS NOT NULL "
                "  AND tariff NOT IN ('legacy', 'admin') "
                "  AND tariff_expires_at IS NOT NULL "
                "  AND tariff_expires_at <= NOW() "
                "  AND tariff_expires_at >= NOW() - ($1::int || ' hours')::interval "
                "  AND (expired_notified_for IS NULL "
                "       OR expired_notified_for <> tariff_expires_at)",
                lookback_hours,
            )
            return [dict(r) for r in rows]
        return await self._execute(_op)

    async def mark_expiry_warned(
        self, user_id: int, expires_at,
    ) -> None:
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET expiry_warned_for = $2 WHERE id = $1",
                user_id, expires_at,
            )
        await self._execute(_op, idempotent=False)

    async def mark_expired_notified(
        self, user_id: int, expires_at,
    ) -> None:
        async def _op(conn):
            await conn.execute(
                "UPDATE users SET expired_notified_for = $2 WHERE id = $1",
                user_id, expires_at,
            )
        await self._execute(_op, idempotent=False)

    async def prune_sent_items(self, days: int = 30) -> int:
        """Delete sent_items rows older than `days`. Returns deleted count.

        152-ФЗ §5(4) data minimization: we only need recent history
        to dedup notifications — items older than ~30 days won't
        re-appear in marketplace feeds anyway. Without a prune job
        this table grows unbounded (~80 items × N subs × 1 cycle/min)
        and accumulates a per-user shopping-history footprint that's
        legally exposed PII over time.
        """
        async def _op(conn):
            row = await conn.fetchrow(
                "WITH d AS ("
                "  DELETE FROM sent_items "
                "  WHERE sent_at < NOW() - ($1::int || ' days')::interval "
                "  RETURNING 1"
                ") SELECT COUNT(*) AS n FROM d",
                days,
            )
            return int(row["n"] if row else 0)
        return await self._execute(_op)

    async def export_user_data(self, user_id: int) -> dict | None:
        """152-ФЗ Art. 14 right of access — full data dump for the user.

        Returns a JSON-serializable dict with the user's profile,
        subscriptions (incl. soft-deleted), and payment history.
        Returns None if the user_id doesn't exist.
        """
        async def _op(conn):
            user = await conn.fetchrow(
                "SELECT id, telegram_id, username, email, lang, currency, "
                "       timezone, onboarded, tariff, tariff_expires_at, "
                "       trial_used, created_at "
                "FROM users WHERE id = $1",
                user_id,
            )
            if not user:
                return None
            subs = await conn.fetch(
                "SELECT id, url, source, name, is_active, deleted, "
                "       error_count, last_error, last_checked_at, "
                "       created_at "
                "FROM subscriptions WHERE user_id = $1 ORDER BY created_at",
                user_id,
            )
            pays = await conn.fetch(
                "SELECT telegram_charge_id, tariff_id, amount_minor, "
                "       currency, created_at "
                "FROM payments WHERE user_id = $1 ORDER BY created_at",
                user_id,
            )
            return {
                "user": dict(user),
                "subscriptions": [dict(s) for s in subs],
                "payments": [dict(p) for p in pays],
            }
        return await self._execute(_op)

    async def delete_user_data(self, user_id: int) -> dict:
        """152-ФЗ Art. 14 right of erasure — hard-delete the user.

        Subscriptions and sent_items cascade on FK. Payments are
        retained but anonymized (user_id → -1) because НК РФ requires
        merchants to keep fiscal records for 4 years; anonymizing the
        link to a person satisfies both 152-ФЗ data minimization and
        the tax-retention obligation.

        Trial usage history is lost with the user row — re-registration
        from the same Telegram account would get a fresh trial. This is
        accepted: an attacker exploiting it pays via SIM rotation
        anyway, and we'd rather honor erasure cleanly than carry a
        deletion-resistant blocklist that itself becomes PII.
        """
        async def _op(conn):
            async with conn.transaction():
                # Capture telegram_id BEFORE the DELETE so we can
                # tombstone it. Without the tombstone, a late YooKassa
                # webhook for a charge made before deletion would
                # reincarnate the user via get_or_create_user (152-ФЗ
                # Art. 14 erasure breach).
                tg_id = await conn.fetchval(
                    "SELECT telegram_id FROM users WHERE id = $1",
                    user_id,
                )
                payments_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM payments WHERE user_id = $1",
                    user_id,
                ) or 0
                if payments_count:
                    await conn.execute(
                        "UPDATE payments SET user_id = -1 WHERE user_id = $1",
                        user_id,
                    )
                subs_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM subscriptions WHERE user_id = $1",
                    user_id,
                ) or 0
                sent_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM sent_items "
                    "WHERE subscription_id IN "
                    "  (SELECT id FROM subscriptions WHERE user_id = $1)",
                    user_id,
                ) or 0
                # ON DELETE CASCADE on subscriptions and sent_items wipes
                # them when the user row is removed.
                await conn.execute("DELETE FROM users WHERE id = $1", user_id)
                if tg_id is not None:
                    await conn.execute(
                        "INSERT INTO tombstoned_users (telegram_id) "
                        "VALUES ($1) ON CONFLICT (telegram_id) DO NOTHING",
                        tg_id,
                    )
                return {
                    "payments_anonymized": int(payments_count),
                    "subscriptions_deleted": int(subs_count),
                    "sent_items_deleted": int(sent_count),
                    "telegram_id_tombstoned": tg_id,
                }
        return await self._execute(_op, idempotent=False)

    async def is_telegram_id_tombstoned(self, telegram_id: int) -> bool:
        """True iff this telegram_id has previously requested erasure.
        Late webhooks / incoming messages for tombstoned ids must NOT
        recreate the user row (152-ФЗ Art. 14)."""
        async def _op(conn):
            row = await conn.fetchval(
                "SELECT 1 FROM tombstoned_users WHERE telegram_id = $1",
                telegram_id,
            )
            return row is not None
        return await self._execute(_op)

    async def log_subject_request(
        self, telegram_id: int, request_type: str,
        outcome: dict | None = None,
    ) -> None:
        """Append-only audit row for 152-ФЗ Art. 18.1 access-request
        register. Called from /export_my_data and /delete_my_account."""
        payload = orjson.dumps(outcome or {}).decode("utf-8")
        async def _op(conn):
            await conn.execute(
                "INSERT INTO data_subject_requests "
                "(telegram_id, request_type, completed_at, outcome) "
                "VALUES ($1, $2, NOW(), $3::jsonb)",
                telegram_id, request_type, payload,
            )
        await self._execute(_op, idempotent=False)

    async def log_admin_access(
        self, admin_telegram_id: int, action: str,
        target_telegram_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        """Append-only audit row for admin actions that surface
        customer PII. Called from /admin user, /admin users, /admin."""
        payload = orjson.dumps(details or {}).decode("utf-8")
        async def _op(conn):
            await conn.execute(
                "INSERT INTO admin_access_log "
                "(admin_telegram_id, action, target_telegram_id, details) "
                "VALUES ($1, $2, $3, $4::jsonb)",
                admin_telegram_id, action, target_telegram_id, payload,
            )
        await self._execute(_op, idempotent=False)

    async def deactivate_user_tariff(self, user_id: int) -> None:
        """Force the user's tariff back to free state. Used by the
        webhook handler when YooKassa reports a refund — the customer
        got their money back, so we revoke access immediately. Trial
        history is preserved (trial_used stays as-is) so they can't
        re-claim the freebie.

        Also pauses all the user's active subscriptions so the
        scheduler stops fetching marketplace pages on their behalf —
        previously a refunded user kept burning proxy/parser cycles
        until their subs hit their own error budget. Subscriptions
        are paused (is_active=FALSE), not deleted, so when they
        re-subscribe the URLs come back with one /start.
        """
        async def _op(conn):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE users SET tariff = NULL, tariff_expires_at = NULL "
                    "WHERE id = $1", user_id,
                )
                await conn.execute(
                    "UPDATE subscriptions SET is_active = FALSE "
                    "WHERE user_id = $1 AND is_active = TRUE AND deleted = FALSE",
                    user_id,
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

        NB: for the YooKassa webhook path, prefer
        `record_and_activate_payment` — it folds the INSERT and the
        tariff UPDATE into one transaction so the user can never end up
        with a recorded payment but no activation (which is what would
        happen here if the caller crashes between record_payment and
        activate_tariff, or if `_execute`'s retry-on-network-error path
        triggers after the INSERT committed but before the client got
        the ack).
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

    async def record_and_activate_payment(
        self, *,
        telegram_charge_id: str, provider_charge_id: str | None,
        user_id: int, tariff_id: str,
        amount_minor: int, currency: str,
        hours: int,
    ) -> tuple[bool, datetime | None]:
        """Atomically record a paid charge AND extend the user's tariff.

        Returns (was_new, new_expiry):
        - was_new=True  → first time we see this charge; tariff was
                          extended in this call. new_expiry is the
                          updated `tariff_expires_at`.
        - was_new=False → duplicate delivery (YooKassa retried, or our
                          `_execute` retried after a network blip that
                          actually committed). The tariff was extended
                          in a *previous* call, so we MUST NOT extend
                          again. new_expiry is None — the caller should
                          treat this as "ack and move on".
        - was_new=None  → user row missing (raced with /delete_my_account
                          or never existed). NO payment recorded. The
                          webhook translates this to HTTP 500 so YooKassa
                          retries; if the user re-registers in the
                          meantime, the retry succeeds. Otherwise the
                          operator must reconcile manually.

        Why this exists (was a real bug before this method):
        - The previous flow was `record_payment()` then a separate
          `activate_tariff()`. If `record_payment` succeeded server-side
          but the client never got the ack, the connection-error retry
          in `_execute` would re-run the INSERT, hit ON CONFLICT, and
          return False — telling the webhook "already activated", but
          activate_tariff was never actually called. Customer paid, no
          tariff. Catastrophic.
        - The previous activate_tariff for paid tariffs did
          SELECT-then-UPDATE without a row lock. Two concurrent webhooks
          for the same user (legitimate: e.g. two payments completing
          within milliseconds) both read the same `current_exp`, both
          computed `current_exp + hours`, second UPDATE overwrote the
          first → user lost a paid month.

        Both classes of bug collapse here:
        - INSERT and UPDATE share one transaction. Failure → both
          rolled back, retry safe. Success → both committed.
        - The UPDATE uses GREATEST(NOW(), COALESCE(...)) so concurrent
          extends stack instead of clobbering each other. Whichever
          UPDATE runs second sees the first's committed value (REPEATABLE
          READ in default isolation isn't enough on its own, but two
          concurrent extends BOTH go through INSERT first; only one wins
          ON CONFLICT, the other returns was_new=False and skips the
          UPDATE entirely).
        """
        async def _op(conn):
            async with conn.transaction():
                # Lock the user row at the top of the transaction. If
                # the user has been erased between webhook arrival and
                # this transaction (race with /delete_my_account), the
                # FOR UPDATE on a missing row returns no result and we
                # bail BEFORE recording the payment. Without this gate,
                # the previous code path INSERTed the payment, then
                # UPDATE matched zero rows, returned (True, None), and
                # the customer's money sat in the books with no tariff.
                user_present = await conn.fetchval(
                    "SELECT 1 FROM users WHERE id = $1 FOR UPDATE",
                    user_id,
                )
                if user_present is None:
                    # Caller (webhook) translates this to a 500 so
                    # YooKassa retries. Either the user is recreated
                    # before then, or the operator manually reconciles.
                    return None, None

                ins = await conn.fetchrow(
                    "INSERT INTO payments "
                    "(telegram_charge_id, provider_charge_id, user_id, "
                    " tariff_id, amount_minor, currency) "
                    "VALUES ($1, $2, $3, $4, $5, $6) "
                    "ON CONFLICT (telegram_charge_id) DO NOTHING "
                    "RETURNING 1",
                    telegram_charge_id, provider_charge_id, user_id,
                    tariff_id, amount_minor, currency,
                )
                if ins is None:
                    # Duplicate — activation already done in a prior call.
                    return False, None

                # Atomic extend: GREATEST guards against two concurrent
                # extends collapsing into one. The user row lock above
                # also serialises concurrent activations for the same
                # user — second tx sees the first's committed expiry.
                row = await conn.fetchrow(
                    "UPDATE users SET tariff = $2, "
                    "  tariff_expires_at = "
                    "    GREATEST(NOW(), COALESCE(tariff_expires_at, NOW())) "
                    "    + ($3::int || ' hours')::interval "
                    "WHERE id = $1 "
                    "RETURNING tariff_expires_at",
                    user_id, tariff_id, hours,
                )
                return True, (row["tariff_expires_at"] if row else None)
        return await self._execute(_op)

    async def get_user_subscriptions(self, user_id: int):
        async def _op(conn):
            return await conn.fetch(
                "SELECT id, url, is_active, created_at, last_checked_at, "
                "       error_count, "
                "       COALESCE(source, 'avito') AS source, name, "
                "       COALESCE(filter_blacklist, '[]'::jsonb) AS filter_blacklist "
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
                "       COALESCE(s.source, 'avito') AS source, u.telegram_id, "
                "       COALESCE(s.filter_blacklist, '[]'::jsonb) AS filter_blacklist "
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
                "       COALESCE(s.source, 'avito') AS source, u.telegram_id, "
                "       COALESCE(s.filter_blacklist, '[]'::jsonb) AS filter_blacklist "
                "FROM subscriptions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.id = $1 AND s.is_active = TRUE AND s.deleted = FALSE",
                sub_id,
            )
        return await self._execute(_op)

    # --- Stop-words (per-subscription blacklist) ---

    # Limits for user-supplied stop-words. 50/30 are generous for any
    # realistic use; without caps a single subscription could swallow
    # arbitrary memory in get_active_subscriptions cycles.
    MAX_BLACKLIST_WORDS = 50
    MAX_BLACKLIST_WORD_LEN = 30
    MIN_BLACKLIST_WORD_LEN = 2

    @staticmethod
    def _normalize_blacklist(words) -> list[str]:
        """Lowercase + trim + dedup + bounds-check input. Tolerates either
        a list or a single comma/newline-separated string from the user."""
        if isinstance(words, str):
            tokens = re.split(r"[,\n;]+", words)
        else:
            tokens = list(words or [])
        seen: set[str] = set()
        out: list[str] = []
        for raw in tokens:
            if not isinstance(raw, str):
                continue
            w = raw.strip().lower()
            if not (Database.MIN_BLACKLIST_WORD_LEN
                    <= len(w) <= Database.MAX_BLACKLIST_WORD_LEN):
                continue
            if w in seen:
                continue
            seen.add(w)
            out.append(w)
            if len(out) >= Database.MAX_BLACKLIST_WORDS:
                break
        return out

    async def get_subscription_blacklist(
        self, sub_id: int, user_id: int,
    ) -> list[str] | None:
        """Return the stop-word list for a subscription, with ownership
        check. Returns None if the sub doesn't exist or isn't owned."""
        async def _op(conn):
            row = await conn.fetchrow(
                "SELECT COALESCE(filter_blacklist, '[]'::jsonb) AS bl "
                "FROM subscriptions "
                "WHERE id = $1 AND user_id = $2 AND deleted = FALSE",
                sub_id, user_id,
            )
            if row is None:
                return None
            raw = row["bl"]
            # asyncpg returns JSONB as str when no codec is registered;
            # parse defensively for either str or list.
            if isinstance(raw, str):
                try:
                    parsed = orjson.loads(raw)
                except Exception:
                    return []
            else:
                parsed = raw
            if not isinstance(parsed, list):
                return []
            return [w for w in parsed if isinstance(w, str)]
        return await self._execute(_op)

    async def set_subscription_blacklist(
        self, sub_id: int, user_id: int, words,
    ) -> bool:
        """Replace the entire blacklist with the normalized input.
        Ownership-checked. Returns True on a real write, False if the
        sub doesn't belong to the user / is deleted."""
        normalized = self._normalize_blacklist(words)
        payload = orjson.dumps(normalized).decode("utf-8")
        async def _op(conn):
            row = await conn.fetchrow(
                "UPDATE subscriptions SET filter_blacklist = $3::jsonb "
                "WHERE id = $1 AND user_id = $2 AND deleted = FALSE "
                "RETURNING id",
                sub_id, user_id, payload,
            )
            return row is not None
        return await self._execute(_op)

    async def deactivate_subscription(self, sub_id: int, user_id: int) -> bool:
        # Ownership-checked soft delete. Both predicates are non-negotiable —
        # the user_id guard is what stops a remote attacker from feeding
        # `del:<random_id>` callbacks via the public Bot API and wiping
        # other people's subscriptions. Returns False when no row matched
        # so the caller can surface "не найдено" instead of pretending the
        # delete succeeded.
        async def _op(conn):
            row = await conn.fetchrow(
                "UPDATE subscriptions SET is_active = FALSE, deleted = TRUE "
                "WHERE id = $1 AND user_id = $2 AND deleted = FALSE "
                "RETURNING id",
                sub_id, user_id,
            )
            return row is not None
        return await self._execute(_op)

    async def toggle_subscription_active(
        self, sub_id: int, user_id: int,
    ) -> str | None:
        """Flip is_active for one of the user's subs. Ownership-checked.

        Returns "paused" if the sub is now is_active=FALSE,
        "resumed" if is_active=TRUE, None if the sub doesn't belong to
        the user / is soft-deleted.

        On resume the error_count is reset and last_error cleared so
        a sub that was paused due to errors gets a clean retry budget.
        """
        async def _op(conn):
            row = await conn.fetchrow(
                "UPDATE subscriptions "
                "SET is_active = NOT is_active, "
                "    error_count = CASE WHEN NOT is_active THEN 0 ELSE error_count END, "
                "    last_error = CASE WHEN NOT is_active THEN NULL ELSE last_error END "
                "WHERE id = $1 AND user_id = $2 AND deleted = FALSE "
                "RETURNING is_active",
                sub_id, user_id,
            )
            if row is None:
                return None
            return "resumed" if row["is_active"] else "paused"
        return await self._execute(_op, idempotent=False)

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
        # Non-idempotent — `error_count = error_count + 1` doubles on a
        # naive retry, which would deactivate the sub a cycle early.
        # Scheduler tolerates a one-cycle miss far better than a wrong
        # deactivation, so we let this fail loudly.
        return await self._execute(_op, idempotent=False)

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
