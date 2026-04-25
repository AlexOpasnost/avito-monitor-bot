"""Source-agnostic types for marketplace parsers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SearchItem:
    """One listing from any marketplace. `source` and `external_id` together
    identify the listing globally (dedup in sent_items)."""
    source: str                       # "avito" | "kufar" | "olx" | ...
    external_id: str                  # unique ID within this source
    title: str
    price: str                        # pretty string, e.g. "15 000 ₽"
    price_value: int | None           # numeric value in the source's currency
    url: str                          # canonical URL to the listing
    image_url: str | None
    location: str | None
    description: str | None
    seller_name: str | None
    published_timestamp: int | None   # unix seconds
    brand: str | None = None          # rendered untranslated as own line
    # Optional listing-attribute fields. Rendered in parens after the title
    # (e.g. "Black jeans (M, Хорошее"). `condition` is translatable;
    # `size` usually isn't (numbers, "M", "XL", "EU 42") so we leave it raw.
    condition: str | None = None
    size: str | None = None
    # Currency code (ISO-4217 upper, e.g. "EUR", "RUB"). Used at render
    # time to convert price into the user's preferred currency.
    currency: str | None = None


@runtime_checkable
class Source(Protocol):
    """Protocol every marketplace parser must implement."""
    name: str

    def matches(self, url: str) -> bool:
        """Return True if this source can handle the URL."""
        ...

    async def fetch(
        self, url: str, proxy: str | None, max_retries: int = 3,
    ) -> list[SearchItem] | None:
        """Fetch current listings for the URL. Return None on failure."""
        ...
