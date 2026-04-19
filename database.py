import asyncio
import asyncpg
import logging
from datetime import datetime, timezone

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
            # Ensure url column is TEXT (unbounded) — older databases may
            # have been created with VARCHAR(N) which truncates long
            # Avito URLs with filter base64 encoded in f=.
            await conn.execute(
                "ALTER TABLE subscriptions ALTER COLUMN url TYPE TEXT"
            )
            logger.info("Migrations applied")
        except Exception as e:
            logger.debug("Migration note: %s", e)

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

    # --- Subscriptions ---

    async def add_subscription(self, user_id: int, url: str) -> int | None:
        async def _op(conn):
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions "
                "WHERE user_id = $1 AND is_active = TRUE AND deleted = FALSE",
                user_id,
            )
            if count >= config.max_subscriptions:
                return None
            row = await conn.fetchrow(
                "INSERT INTO subscriptions (user_id, url) VALUES ($1, $2) RETURNING id",
                user_id, url,
            )
            return row["id"]
        return await self._execute(_op)

    async def get_user_subscriptions(self, user_id: int):
        async def _op(conn):
            return await conn.fetch(
                "SELECT id, url, is_active, created_at, last_checked_at, error_count "
                "FROM subscriptions WHERE user_id = $1 AND deleted = FALSE "
                "ORDER BY created_at DESC",
                user_id,
            )
        return await self._execute(_op)

    async def get_active_subscriptions(self):
        async def _op(conn):
            return await conn.fetch(
                "SELECT s.id, s.url, s.user_id, s.last_checked_at, u.telegram_id "
                "FROM subscriptions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.is_active = TRUE AND s.deleted = FALSE"
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

    async def is_item_sent(self, sub_id: int, avito_id: str) -> bool:
        async def _op(conn):
            return await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM sent_items "
                "WHERE subscription_id = $1 AND avito_id = $2)",
                sub_id, avito_id,
            )
        return await self._execute(_op)

    async def mark_item_sent(self, sub_id: int, avito_id: str):
        async def _op(conn):
            await conn.execute(
                "INSERT INTO sent_items (subscription_id, avito_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                sub_id, avito_id,
            )
        await self._execute(_op)

    async def mark_items_sent_batch(self, sub_id: int, avito_ids: list[str]):
        if not avito_ids:
            return
        async def _op(conn):
            await conn.executemany(
                "INSERT INTO sent_items (subscription_id, avito_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                [(sub_id, aid) for aid in avito_ids if aid],
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
            return {
                "total_users": total_users,
                "active_subs": active_subs,
                "unique_urls": unique_urls,
                "total_sent": total_sent,
                "last_checked": last_checked,
                "new_users_24h": new_users_24h,
                "sent_24h": sent_24h,
            }
        return await self._execute(_op)


db = Database()
