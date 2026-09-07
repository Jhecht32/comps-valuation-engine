"""Suggested peer set for a target.

Industry is the first cut: companies Yahoo classifies alongside the
target. When that leaves fewer than MIN_INDUSTRY_PEERS inside the
market-cap band (a very large or very small company in a thin industry),
the sector is screened as well and those names are appended, each
labelled with the basis it matched on so the user can see which
suggestions are loose.
"""

from app.data.peers import (
    MARKET_CAP_HIGH_MULTIPLE,
    MARKET_CAP_LOW_MULTIPLE,
    MarketCapBand,
    MatchBasis,
    market_cap_band,
    select_peers,
)
from app.data.provider import CompanyProfile, MarketDataProvider
from app.services.comps import normalise_ticker
from app.services.errors import InsufficientDataError
from app.services.models import PeerSuggestions, SuggestedPeer

MIN_INDUSTRY_PEERS = 3
DEFAULT_LIMIT = 25
_SCREEN_SIZE = 100  # ask the source for more than the limit; the rule drops some


def _screen(
    provider: MarketDataProvider,
    target: CompanyProfile,
    band: MarketCapBand,
    basis: MatchBasis,
) -> list[SuggestedPeer]:
    candidates = provider.screen_peers(
        sector=target.sector,
        industry=target.industry if basis is MatchBasis.INDUSTRY else None,
        market_cap_min=band.low,
        market_cap_max=band.high,
        limit=_SCREEN_SIZE,
    )
    selected = select_peers(
        target,
        candidates,
        basis=basis,
        low_multiple=band.low_multiple,
        high_multiple=band.high_multiple,
    )
    return [SuggestedPeer(**c.model_dump(), match_basis=basis) for c in selected]


def suggest_peers(
    provider: MarketDataProvider,
    ticker: str,
    *,
    low_multiple: float = MARKET_CAP_LOW_MULTIPLE,
    high_multiple: float = MARKET_CAP_HIGH_MULTIPLE,
    limit: int = DEFAULT_LIMIT,
) -> PeerSuggestions:
    key = normalise_ticker(ticker)
    profile = provider.get_profile(key)
    if profile.market_cap is None or profile.market_cap <= 0:
        raise InsufficientDataError(key, "market cap unavailable; cannot size a peer band")
    if profile.industry is None and profile.sector is None:
        raise InsufficientDataError(key, "sector and industry unknown; nothing to screen on")

    band = market_cap_band(profile.market_cap, low_multiple=low_multiple, high_multiple=high_multiple)

    peers: list[SuggestedPeer] = []
    if profile.industry is not None:
        peers.extend(_screen(provider, profile, band, MatchBasis.INDUSTRY))
    if len(peers) < MIN_INDUSTRY_PEERS and profile.sector is not None:
        seen = {p.ticker for p in peers}
        peers.extend(p for p in _screen(provider, profile, band, MatchBasis.SECTOR) if p.ticker not in seen)

    return PeerSuggestions(target=profile, market_cap_band=band, peers=peers[:limit])
