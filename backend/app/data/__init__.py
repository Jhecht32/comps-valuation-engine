"""Market data providers: fetch from a source, map into CompanySnapshot.

The valuation module never imports from here; the dependency runs one
way (data -> valuation) so the engine stays network-free.
"""

from app.data.cache import CachedProvider
from app.data.fixtures import FixtureProvider
from app.data.provider import (
    CurrencyMismatchError,
    MarketDataError,
    MarketDataProvider,
    Quote,
    TickerNotFoundError,
    raise_for_currency_mismatch,
    reject_currency_mismatches,
)

__all__ = [
    "CachedProvider",
    "CurrencyMismatchError",
    "FixtureProvider",
    "MarketDataError",
    "MarketDataProvider",
    "Quote",
    "TickerNotFoundError",
    "raise_for_currency_mismatch",
    "reject_currency_mismatches",
]
