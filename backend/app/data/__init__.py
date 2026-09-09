"""Market data providers: fetch from a source, map into CompanySnapshot.

The valuation module never imports from here; the dependency runs one
way (data -> valuation) so the engine stays network-free.
"""

from app.data.cache import CachedProvider
from app.data.fixtures import FixtureProvider
from app.data.peers import MarketCapBand, MatchBasis, market_cap_band, same_currency, select_peers
from app.data.provider import (
    CompanyProfile,
    CurrencyMismatchError,
    MarketDataError,
    MarketDataProvider,
    PeerDiscoveryUnavailableError,
    Quote,
    TickerNotFoundError,
    raise_for_currency_mismatch,
    reject_currency_mismatches,
)
from app.data.proxy_peers import (
    FixtureProxyPeerSource,
    ProxyPeerGroup,
    ProxyPeersError,
    ProxyPeerSource,
)

__all__ = [
    "CachedProvider",
    "CompanyProfile",
    "CurrencyMismatchError",
    "FixtureProvider",
    "FixtureProxyPeerSource",
    "MarketCapBand",
    "MarketDataError",
    "MarketDataProvider",
    "MatchBasis",
    "PeerDiscoveryUnavailableError",
    "ProxyPeerGroup",
    "ProxyPeerSource",
    "ProxyPeersError",
    "Quote",
    "TickerNotFoundError",
    "market_cap_band",
    "raise_for_currency_mismatch",
    "reject_currency_mismatches",
    "same_currency",
    "select_peers",
]
