"""Tests for peer discovery in the data layer: the CompanyProfile
contract, the market-cap band, the pure select_peers rule, and the
provider plumbing (base defaults, fixture provider, cache delegation).

The rule is deliberately mechanical: same industry (or sector), market
cap within 0.25x-4.0x of the target, same currencies. The banker still
picks the final set; this only proposes candidates.
"""

from datetime import timedelta

import pytest

from app.data import CachedProvider, FixtureProvider, TickerNotFoundError
from app.data.fixtures import HOME_DEPOT
from app.data.peers import (
    MARKET_CAP_HIGH_MULTIPLE,
    MARKET_CAP_LOW_MULTIPLE,
    MatchBasis,
    market_cap_band,
    select_peers,
)
from app.data.provider import (
    CompanyProfile,
    MarketDataProvider,
    PeerDiscoveryUnavailableError,
)

B = 1_000_000_000.0


def profile(ticker, cap, *, industry="Home Improvement Retail", sector="Consumer Cyclical",
            price_currency="USD", reporting_currency="USD"):
    return CompanyProfile(
        ticker=ticker,
        name=f"{ticker} Inc.",
        sector=sector,
        industry=industry,
        market_cap=cap,
        price_currency=price_currency,
        reporting_currency=reporting_currency,
        exchange="NYQ",
    )


HD = profile("HD", 320 * B)


# --- Market-cap band ----------------------------------------------------


def test_band_is_quarter_to_four_times_target():
    band = market_cap_band(100 * B)
    assert band.low == 25 * B
    assert band.high == 400 * B
    assert band.low_multiple == MARKET_CAP_LOW_MULTIPLE == 0.25
    assert band.high_multiple == MARKET_CAP_HIGH_MULTIPLE == 4.0


def test_band_accepts_custom_multiples():
    band = market_cap_band(100 * B, low_multiple=0.5, high_multiple=2.0)
    assert (band.low, band.high) == (50 * B, 200 * B)


@pytest.mark.parametrize("cap", [None, 0.0, -1.0])
def test_band_rejects_missing_or_nonpositive_cap(cap):
    with pytest.raises(ValueError):
        market_cap_band(cap)


# --- select_peers -------------------------------------------------------


def test_keeps_same_industry_inside_band_ordered_by_size_similarity():
    # Ordered by how close each market cap is to the target's on a log
    # scale, so a mid-cap's list starts with names its own size rather
    # than the largest companies the band admits.
    candidates = [
        profile("FND", 5 * B),        # too small (< 80B)
        profile("LOW", 115 * B),      # ratio 0.36
        profile("BIG", 1_000 * B),    # ratio 3.1
        profile("HUGE", 2_000 * B),   # too big (> 1,280B)
        profile("MID", 90 * B),       # ratio 0.28
        profile("NEAR", 400 * B),     # ratio 1.25
    ]
    peers = select_peers(HD, candidates, basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["NEAR", "LOW", "BIG", "MID"]


def test_band_edges_are_inclusive():
    candidates = [profile("LO", 80 * B), profile("HI", 1_280 * B)]
    peers = select_peers(HD, candidates, basis=MatchBasis.INDUSTRY)
    assert {p.ticker for p in peers} == {"LO", "HI"}


def test_excludes_target_itself_and_duplicates():
    candidates = [HD, profile("LOW", 115 * B), profile("LOW", 115 * B)]
    peers = select_peers(HD, candidates, basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["LOW"]


def test_dual_share_classes_collapse_to_the_larger_listing():
    # LEN and LEN-B are one company; two rows would double-count it in
    # the median. Keep the listing with the larger market cap.
    lennar = profile("LEN", 100 * B).model_copy(update={"name": "Lennar Corporation"})
    lennar_b = profile("LEN-B", 99 * B).model_copy(update={"name": "Lennar Corporation"})
    other = profile("LOW", 115 * B)
    peers = select_peers(HD, [lennar_b, other, lennar], basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["LOW", "LEN"]


def test_targets_other_share_class_is_not_its_own_peer():
    # GOOGL's peer list must not contain GOOG: same company, same multiples.
    googl = profile("GOOGL", 2_000 * B, industry="Internet Content & Information").model_copy(
        update={"name": "Alphabet Inc."}
    )
    goog = googl.model_copy(update={"ticker": "GOOG", "market_cap": 1_990 * B})
    meta = profile("META", 1_500 * B, industry="Internet Content & Information").model_copy(
        update={"name": "Meta Platforms, Inc."}
    )
    peers = select_peers(googl, [googl, goog, meta], basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["META"]


def test_unnamed_candidates_are_not_collapsed_together():
    a = profile("AAA", 100 * B).model_copy(update={"name": None})
    b = profile("BBB", 90 * B).model_copy(update={"name": None})
    peers = select_peers(HD, [a, b], basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["AAA", "BBB"]


def test_industry_basis_drops_other_industries_even_in_sector():
    candidates = [profile("TSCO", 100 * B, industry="Specialty Retail")]
    assert select_peers(HD, candidates, basis=MatchBasis.INDUSTRY) == []


def test_sector_basis_accepts_any_industry_in_sector():
    candidates = [
        profile("TSCO", 100 * B, industry="Specialty Retail"),
        profile("XOM", 100 * B, industry="Oil & Gas Integrated", sector="Energy"),
    ]
    peers = select_peers(HD, candidates, basis=MatchBasis.SECTOR)
    assert [p.ticker for p in peers] == ["TSCO"]


def test_drops_candidates_without_market_cap():
    candidates = [profile("NOCAP", None), profile("LOW", 115 * B)]
    peers = select_peers(HD, candidates, basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["LOW"]


def test_drops_candidates_in_another_currency():
    # A foreign ordinary quoted in USD on the pink sheets but reporting in
    # AUD would be rejected by the currency guard at comps time anyway.
    candidates = [
        profile("WFAFF", 100 * B, reporting_currency="AUD"),
        profile("SHOP", 100 * B, price_currency="CAD", reporting_currency="CAD"),
        profile("UNK", 100 * B, price_currency=None, reporting_currency=None),
        profile("LOW", 115 * B),
    ]
    peers = select_peers(HD, candidates, basis=MatchBasis.INDUSTRY)
    assert [p.ticker for p in peers] == ["LOW"]


def test_target_without_industry_yields_nothing_on_industry_basis():
    target = profile("HD", 320 * B, industry=None)
    assert select_peers(target, [profile("LOW", 115 * B)], basis=MatchBasis.INDUSTRY) == []


def test_target_without_market_cap_raises():
    target = profile("HD", None)
    with pytest.raises(ValueError):
        select_peers(target, [profile("LOW", 115 * B)], basis=MatchBasis.INDUSTRY)


# --- Provider plumbing --------------------------------------------------


class SnapshotOnlyProvider(MarketDataProvider):
    def fetch_company(self, ticker):
        return HOME_DEPOT.model_copy(deep=True)


def test_base_provider_has_no_peer_discovery_by_default():
    p = SnapshotOnlyProvider()
    with pytest.raises(PeerDiscoveryUnavailableError):
        p.get_profile("HD")
    with pytest.raises(PeerDiscoveryUnavailableError):
        p.screen_peers(sector="Consumer Cyclical", industry=None, market_cap_min=1.0, market_cap_max=2.0)


@pytest.fixture
def fixture_provider():
    profiles = {
        "HD": HD,
        "LOW": profile("LOW", 115 * B),
        "FND": profile("FND", 5 * B),
        "TSCO": profile("TSCO", 18 * B, industry="Specialty Retail"),
    }
    return FixtureProvider(profiles=profiles)


def test_fixture_provider_serves_profiles(fixture_provider):
    assert fixture_provider.get_profile(" hd ").ticker == "HD"
    with pytest.raises(TickerNotFoundError):
        fixture_provider.get_profile("NOPE")


def test_fixture_provider_screens_by_industry_and_band(fixture_provider):
    found = fixture_provider.screen_peers(
        sector="Consumer Cyclical", industry="Home Improvement Retail",
        market_cap_min=80 * B, market_cap_max=1_280 * B,
    )
    assert {p.ticker for p in found} == {"HD", "LOW"}


def test_fixture_provider_screens_by_sector_when_no_industry(fixture_provider):
    found = fixture_provider.screen_peers(
        sector="Consumer Cyclical", industry=None,
        market_cap_min=1 * B, market_cap_max=1_000 * B,
    )
    assert {p.ticker for p in found} == {"HD", "LOW", "FND", "TSCO"}


def test_fixture_provider_without_profiles_reports_unavailable():
    p = FixtureProvider()
    with pytest.raises(PeerDiscoveryUnavailableError):
        p.get_profile("HD")


def test_cached_provider_delegates_peer_discovery(fixture_provider):
    cached = CachedProvider(fixture_provider, price_ttl=timedelta(minutes=1))
    assert cached.get_profile("HD") == HD
    found = cached.screen_peers(
        sector="Consumer Cyclical", industry="Home Improvement Retail",
        market_cap_min=80 * B, market_cap_max=1_280 * B,
    )
    assert {p.ticker for p in found} == {"HD", "LOW"}
