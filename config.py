import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str = ""
    database_url: str = ""
    proxy_list: list[str] = field(default_factory=list)
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
    # Paywall
    basic_price_rub: int = 990
    pro_price_rub: int = 2490
    payment_url_basic: str = ""     # real payment link (YooMoney / Robokassa / …)
    payment_url_pro: str = ""
    support_handle: str = ""        # e.g. "@autosearch_support"
    # Telegram Payments provider token — kept for back-compat. The
    # paywall now uses the YooKassa REST API path (supports СБП, all
    # methods); this token is only checked as a feature flag.
    payment_provider_token: str = ""
    # YooKassa REST API credentials. Used to create payments
    # (POST /v3/payments) and verify webhooks (GET /v3/payments/{id}).
    # Both required for paid tariffs.
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    # Public HTTPS URL of THIS bot's webhook endpoint, e.g.
    # https://avito-monitor-bot-production.up.railway.app
    # Configured in YooKassa dashboard → Notifications →
    # https://<webhook_base_url>/webhook/yookassa
    webhook_base_url: str = ""
    # Local port the aiohttp webhook server listens on. Railway
    # injects $PORT for the public-facing service; we honour that
    # at startup if set.
    webhook_port: int = 8000
    # CIDR ranges allowed to POST to /webhook/yookassa. YooKassa
    # publishes its outbound IPs at
    # https://yookassa.ru/developers/using-api/webhooks#ip — keep
    # this in sync if they change. Empty list = no IP gate (fall
    # back to the GET-back verification only). Override with the
    # YOOKASSA_ALLOWED_IPS env var (comma-separated CIDRs).
    yookassa_allowed_ips: list[str] = field(default_factory=lambda: [
        "185.71.76.0/27",
        "185.71.77.0/27",
        "77.75.153.0/25",
        "77.75.154.0/25",
        "77.75.156.11/32",
        "77.75.156.35/32",
        "2a02:5180::/32",
    ])
    # Compliance kill-switch — names of marketplace sources that
    # are temporarily disabled (e.g. after a cease-and-desist
    # notice). Add via DISABLED_SOURCES=avito,kufar env var, save,
    # service redeploys in ~30s and parsing stops. New subscriptions
    # to those sources are also rejected.
    disabled_sources: list[str] = field(default_factory=list)
    # Public URLs for the legal documents shown in /start and /help.
    # Defaults point at the canonical GitHub Pages publication so the
    # legal footer ALWAYS renders (was previously hidden when env vars
    # were empty — meaning the bot collected PII without a visible
    # consent text, a 152-ФЗ Art. 9 issue we fixed in May 2026).
    # Override via env if the operator publishes elsewhere.
    privacy_url: str = "https://alexopasnost.github.io/avito-monitor-bot/PRIVACY"
    offer_url: str = "https://alexopasnost.github.io/avito-monitor-bot/OFFER"
    # Stamp recorded against `users.consent_policy_version` when a
    # user finishes onboarding. Bump on substantive PRIVACY/OFFER
    # changes — gives the operator an audit trail of which version
    # each user agreed to.
    #
    # v3-2026-05-02 — added channel-subscribe gate (PRIVACY §2/§3/§4.2
    # mention getChatMember and channel-membership processing) + new
    # tariff prices in OFFER §3 (1290/1990/2990 ₽).
    consent_policy_version: str = "v3-2026-05-02"
    # Self-employed (НПД) annual income limit in rubles. The admin
    # dashboard alerts at 90% and new sales are soft-blocked at 100%
    # so the operator never accidentally crosses the cap (which would
    # force a switch to ИП and back-tax penalties).
    npd_annual_limit_rub: int = 2_400_000
    # Sentry DSN for crash + error reporting. Empty string = Sentry
    # disabled (we ship a `sentry_sdk.init` only when this is set so
    # the dependency stays optional and dev installs don't need it).
    sentry_dsn: str = ""
    # Optional environment label propagated to Sentry events
    # (production / staging / dev). Defaults to "production" because
    # that's where bugs that matter actually fire.
    sentry_environment: str = "production"
    # Channel-subscribe gate shown on /start. Either a public username
    # ("@autoserch") or a numeric chat id ("-100…"). Empty = gate
    # disabled. The bot MUST be an administrator in the channel —
    # otherwise getChatMember fails and the gate fails open (lets the
    # user through with a logged warning, so a misconfigured admin
    # doesn't lock the entire userbase out).
    required_channel: str = ""

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
            max_subscriptions=int(os.getenv("MAX_SUBSCRIPTIONS", "5")),
            telegram_api_url=os.getenv("TELEGRAM_API_URL", "https://api.telegram.org"),
            proxy_rotate_url=os.getenv("PROXY_ROTATE_URL", ""),
            admin_id=single_admin,
            admin_ids=admin_ids,
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", "3")),
            headless=os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes"),
            basic_price_rub=int(os.getenv("BASIC_PRICE_RUB", "990")),
            pro_price_rub=int(os.getenv("PRO_PRICE_RUB", "2490")),
            payment_url_basic=os.getenv("PAYMENT_URL_BASIC", ""),
            payment_url_pro=os.getenv("PAYMENT_URL_PRO", ""),
            support_handle=os.getenv("SUPPORT_HANDLE", ""),
            payment_provider_token=os.getenv("PAYMENT_PROVIDER_TOKEN", ""),
            yookassa_shop_id=os.getenv("YOOKASSA_SHOP_ID", "").strip(),
            yookassa_secret_key=os.getenv("YOOKASSA_SECRET_KEY", "").strip(),
            webhook_base_url=os.getenv("WEBHOOK_BASE_URL", "").rstrip("/"),
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8000")),
            yookassa_allowed_ips=([
                s.strip()
                for s in os.getenv("YOOKASSA_ALLOWED_IPS", "").split(",")
                if s.strip()
            ] or [
                # Default = YooKassa's documented production IPs.
                "185.71.76.0/27",
                "185.71.77.0/27",
                "77.75.153.0/25",
                "77.75.154.0/25",
                "77.75.156.11/32",
                "77.75.156.35/32",
                "2a02:5180::/32",
            ]),
            disabled_sources=[
                s.strip().lower()
                for s in os.getenv("DISABLED_SOURCES", "").split(",")
                if s.strip()
            ],
            # Empty env values fall through to the dataclass defaults
            # so the legal footer always renders even when the operator
            # forgot to copy these into Railway.
            privacy_url=(
                os.getenv("PRIVACY_URL", "").strip()
                or "https://alexopasnost.github.io/avito-monitor-bot/PRIVACY"
            ),
            offer_url=(
                os.getenv("OFFER_URL", "").strip()
                or "https://alexopasnost.github.io/avito-monitor-bot/OFFER"
            ),
            consent_policy_version=os.getenv(
                "CONSENT_POLICY_VERSION", "v3-2026-05-02",
            ).strip(),
            npd_annual_limit_rub=int(
                os.getenv("NPD_ANNUAL_LIMIT_RUB", "2400000")
            ),
            sentry_dsn=os.getenv("SENTRY_DSN", "").strip(),
            sentry_environment=os.getenv("SENTRY_ENVIRONMENT", "production").strip(),
            required_channel=os.getenv("REQUIRED_CHANNEL", "").strip(),
        )


config = Config.from_env()
