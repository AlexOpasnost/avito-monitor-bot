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
    # Avito mobile API
    avito_api_key: str = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
    webapp_url: str = ""
    max_concurrent_requests: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
    )

    @classmethod
    def from_env(cls) -> "Config":
        proxies_raw = os.getenv("PROXY_LIST", "")
        proxies = [p.strip() for p in proxies_raw.split(",") if p.strip()]
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            database_url=os.getenv("DATABASE_URL", "postgresql://localhost/avito_monitor"),
            proxy_list=proxies,
            parse_interval=int(os.getenv("PARSE_INTERVAL", "60")),
            max_subscriptions=int(os.getenv("MAX_SUBSCRIPTIONS", "5")),
            telegram_api_url=os.getenv("TELEGRAM_API_URL", "https://api.telegram.org"),
            proxy_rotate_url=os.getenv("PROXY_ROTATE_URL", ""),
            admin_id=int(os.getenv("ADMIN_ID", "0")),
            webapp_url=os.getenv("WEBAPP_URL", ""),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", "3")),
        )


config = Config.from_env()
