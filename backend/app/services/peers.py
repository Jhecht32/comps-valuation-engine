"""Suggested peer set for a target.

Industry is the only screen: companies Yahoo classifies in the target's
industry, inside the market-cap band, in the target's currencies. Sector
is deliberately not a fallback. A sector such as "Consumer Cyclical" puts
restaurants, hotels, and online travel next to a home-improvement
retailer, and a peer set padded with those is worse than a short one.
When the industry yields fewer than MIN_PEERS names the result is
returned as found and flagged THIN_PEER_SET; the user adds names.
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
from app.valuation.models import DataQualityFlag, FlagCode

MIN_PEERS = 3
DEFAULT_LIMIT = 25
_SCREEN_SIZE = 100  # ask the source for more than the limit; the rule drops some


def _screen(provider: MarketDataProvider, target: CompanyProfile, band: MarketCapBand) -> list[SuggestedPeer]:
    candidates = provider.screen_peers(
        sector=target.sector,
        industry=target.industry,
        market_cap_min=band.low,
        market_cap_max=band.high,
        limit=_SCREEN_SIZE,
    )
    selected = select_peers(
        target,
        candidates,
        basis=MatchBasis.INDUSTRY,
        low_multiple=band.low_multiple,
        high_multiple=band.high_multiple,
    )
    return [SuggestedPeer(**c.model_dump(), match_basis=MatchBasis.INDUSTRY) for c in selected]


def _multiple(x: float) -> str:
    text = f"{x:.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text  # 0.33, 3.0


def _thin_flag(target: CompanyProfile, band: MarketCapBand, found: int) -> DataQualityFlag:
    return DataQualityFlag(
        code=FlagCode.THIN_PEER_SET,
        message=(
            f"Only {found} {target.industry} name{'' if found == 1 else 's'} within "
            f"{_multiple(band.low_multiple)}x-{_multiple(band.high_multiple)}x of {target.ticker}'s market cap; "
            f"fewer than {MIN_PEERS} is a thin set. The sector is not used as a fallback; "
            "add peers by hand."
        ),
    )


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
    if profile.industry is None:
        raise InsufficientDataError(key, "industry unknown; cannot screen for peers")

    band = market_cap_band(profile.market_cap, low_multiple=low_multiple, high_multiple=high_multiple)
    peers = _screen(provider, profile, band)[:limit]
    flags = [_thin_flag(profile, band, len(peers))] if len(peers) < MIN_PEERS else []
    return PeerSuggestions(target=profile, market_cap_band=band, peers=peers, flags=flags)
