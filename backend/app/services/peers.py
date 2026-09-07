"""Suggested peer set for a target.

Industry is the first screen: companies Yahoo classifies in the target's
industry, inside the market-cap band, in the target's currencies. Fewer
than MIN_PEERS names would leave the statistics resting on one or two
multiples, so the screen then widens to the sector, adding the sector
names closest in market cap until the set holds MAX_WIDENED, and flags
the result PEER_SET_WIDENED: a sector like "Consumer Cyclical" puts
restaurants and hotels next to a home-improvement retailer, and those
matches need review. A set still short of MIN_PEERS after widening also
carries THIN_PEER_SET. A single name is never returned unflagged.
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

MIN_PEERS = 4  # fewer industry names than this widens the screen to the sector
MAX_WIDENED = 8  # cap on a widened set; sector names closest in market cap fill it
DEFAULT_LIMIT = 25
_SCREEN_SIZE = 100  # ask the source for more than the limit; the rule drops some


def _screen(
    provider: MarketDataProvider, target: CompanyProfile, band: MarketCapBand, basis: MatchBasis
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


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _multiple(x: float) -> str:
    text = f"{x:.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text  # 0.33, 3.0


def _widened_flag(target: CompanyProfile, *, industry_found: int, added: int) -> DataQualityFlag:
    if target.industry is None:
        found = f"{target.ticker} has no industry classification, so the {target.sector} sector was screened instead"
    else:
        found = (
            f"The industry screen found {_plural(industry_found, target.industry + ' name')}, fewer than "
            f"{MIN_PEERS}, so the set was widened to the {target.sector} sector"
        )
    return DataQualityFlag(
        code=FlagCode.PEER_SET_WIDENED,
        message=(
            f"{found}, adding {_plural(added, 'name')} closest in market cap (cap {MAX_WIDENED}). "
            "Sector matches are loose; review the set before relying on it."
        ),
    )


def _thin_flag(target: CompanyProfile, band: MarketCapBand, found: int) -> DataQualityFlag:
    return DataQualityFlag(
        code=FlagCode.THIN_PEER_SET,
        message=(
            f"Only {_plural(found, 'name')} within {_multiple(band.low_multiple)}x-{_multiple(band.high_multiple)}x "
            f"of {target.ticker}'s market cap; fewer than {MIN_PEERS} is a thin set. Add peers by hand."
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
    if profile.industry is None and profile.sector is None:
        raise InsufficientDataError(key, "sector and industry unknown; nothing to screen on")

    band = market_cap_band(profile.market_cap, low_multiple=low_multiple, high_multiple=high_multiple)
    peers = _screen(provider, profile, band, MatchBasis.INDUSTRY) if profile.industry is not None else []
    flags: list[DataQualityFlag] = []

    if len(peers) < MIN_PEERS and profile.sector is not None:
        industry_found = len(peers)
        seen = {p.ticker for p in peers}
        room = max(MAX_WIDENED - industry_found, 0)
        sector_peers = [p for p in _screen(provider, profile, band, MatchBasis.SECTOR) if p.ticker not in seen]
        peers = peers + sector_peers[:room]
        flags.append(_widened_flag(profile, industry_found=industry_found, added=len(peers) - industry_found))

    peers = peers[:limit]
    if len(peers) < MIN_PEERS:
        flags.append(_thin_flag(profile, band, len(peers)))
    return PeerSuggestions(target=profile, market_cap_band=band, peers=peers, flags=flags)
