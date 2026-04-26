import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str = ""
    database_url: str = ""
    proxy_list: list[str] = field(default_factory=list)
    parse_interval: int = 60
    max_subscriptions: int = 5
    max_errors_before_deactivate: int = 10
    telegram_api_url: str = "https://api.telegram.org"
    proxy_rotate_url: str = ""
    admin_id: int = 0
    # Comma-separated list of Telegram user IDs that get admin
    # privileges: full /admin panel + auto-assigned `admin` tariff
    # (999 searches, no expiry, no payment). Populated from
    # ADMIN_IDS env var; falls back to a single-element list built
    # from ADMIN_ID for back-compat.
    admin_ids: list[int] = field(default_factory=list)
    # Avito mobile API
    avito_api_key: str = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
    max_concurrent_requests: int = 3
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
    )
    # Branding + paywall
    brand_name: str = "AutoSearch"
    basic_price_rub: int = 990
    pro_price_rub: int = 2490
    payment_url_basic: str = ""     # real payment link (YooMoney / Robokassa / …)
    payment_url_pro: str = ""
    support_handle: str = ""        # e.g. "@autosearch_support"
    # Telegram Payments provider token (YooKassa / Stripe / …). Set via
    # @BotFather → bot → Payments. Empty string disables paid tariffs —
    # in that case the buy buttons fall back to the support handle.
    payment_provider_token: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        proxies_raw = os.getenv("PROXY_LIST", "")
        proxies = [p.strip() for p in proxies_raw.split(",") if p.strip()]

        # Multi-admin: ADMIN_IDS=123,456,789 (preferred). For
        # back-compat, fall back to a single ADMIN_ID env var.
        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids: list[int] = []
        for chunk in admin_ids_raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                admin_ids.append(int(chunk))
            except ValueError:
                pass
        single_admin = int(os.getenv("ADMIN_ID", "0"))
        if not admin_ids and single_admin:
            admin_ids = [single_admin]
        elif single_admin and single_admin not in admin_ids:
            # Both set → union
            admin_ids.append(single_admin)

        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            database_url=os.getenv("DATABASE_URL", "postgresql://localhost/avito_monitor"),
            proxy_list=proxies,
            parse_interval=int(os.getenv("PARSE_INTERVAL", "60")),
            max_subscriptions=int(os.getenv("MAX_SUBSCRIPTIONS", "5")),
            telegram_api_url=os.getenv("TELEGRAM_API_URL", "https://api.telegram.org"),
            proxy_rotate_url=os.getenv("PROXY_ROTATE_URL", ""),
            admin_id=single_admin,
            admin_ids=admin_ids,
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", "3")),
            headless=os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes"),
            brand_name=os.getenv("BRAND_NAME", "AutoSearch"),
            basic_price_rub=int(os.getenv("BASIC_PRICE_RUB", "990")),
            pro_price_rub=int(os.getenv("PRO_PRICE_RUB", "2490")),
            payment_url_basic=os.getenv("PAYMENT_URL_BASIC", ""),
            payment_url_pro=os.getenv("PAYMENT_URL_PRO", ""),
            support_handle=os.getenv("SUPPORT_HANDLE", ""),
            payment_provider_token=os.getenv("PAYMENT_PROVIDER_TOKEN", ""),
        )


config = Config.from_env()
