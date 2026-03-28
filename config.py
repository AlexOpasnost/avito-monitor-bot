import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str = ""
    database_url: str = ""
    proxy_list: list[str] = field(default_factory=list)
    parse_interval: int = 300  # seconds (5 min)
    max_subscriptions: int = 5
    max_errors_before_deactivate: int = 10
    request_delay_min: float = 3.0
    request_delay_max: float = 10.0
    telegram_api_url: str = "https://api.telegram.org"
    proxy_rotate_url: str = ""
    proxy_rotate_every: int = 5  # rotate IP every N requests

    @classmethod
    def from_env(cls) -> "Config":
        proxies_raw = os.getenv("PROXY_LIST", "")
        proxies = [p.strip() for p in proxies_raw.split(",") if p.strip()]
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            database_url=os.getenv("DATABASE_URL", "postgresql://localhost/avito_monitor"),
            proxy_list=proxies,
            parse_interval=int(os.getenv("PARSE_INTERVAL", "300")),
            max_subscriptions=int(os.getenv("MAX_SUBSCRIPTIONS", "5")),
            telegram_api_url=os.getenv("TELEGRAM_API_URL", "https://api.telegram.org"),
            proxy_rotate_url=os.getenv("PROXY_ROTATE_URL", ""),
            proxy_rotate_every=int(os.getenv("PROXY_ROTATE_EVERY", "5")),
        )


config = Config.from_env()
