"""Tests for the service layer: the composition of provider output into
engine calls. The services own no arithmetic; these tests check that the
right engine functions are called with the right inputs, that engine
refusals become per-ticker errors, and that a comps run survives bad
peers.
"""

from datetime import date

import pytest

from app.data import (
    CompanyProfile,
    FixtureProvider,
    MarketDataError,
    PeerDiscoveryUnavailableError,
    TickerNotFoundError,
)
from app.data.fixtures import HOME_DEPOT
from app.services import (
    ErrorCode,
    InsufficientDataError,
    PeerSource,
    run_comps,
    suggest_peers,
    value_company,
)
from app.valuation import FlagCode, MidBasis, MultipleKind
from tests.helpers import CountingProvider, load_recorded_snapshots, value_snapshot

TODAY = date(2026, 9, 6)
B = 1_000_000_000.0


def hd_like(ticker, **update):
    return HOME_DEPOT.model_copy(update={"ticker": ticker, "name": ticker, **update}, deep=True)


# --- value_company ---------------------------------------------------------


def test_value_company_ties_to_golden_model():
    v = value_company(HOME_DEPOT, today=TODAY)
    assert v.ticker == "HD"
    assert v.name == "The Home Depot, Inc."
    assert v.source == "fixture"
    assert v.share_price == 321.05
    assert v.balance_sheet_as_of == date(2026, 8, 2)
    assert v.bridge.enterprise_value == pytest.approx(370_576.8e6, rel=1e-6)
    assert v.multiples.ev_ebitda.value == pytest.approx(14.628, abs=5e-4)
    assert v.diluted_eps == pytest.approx(14_227e6 / 996e6, rel=1e-6)
    assert v.multiples.pe.value == pytest.approx(321.05 / (14_227 / 996), rel=1e-6)
    assert v.ltm.as_of == date(2026, 8, 2)
    assert v.flags == []


def test_value_company_agrees_with_test_helper_on_recorded_snapshots():
    for ticker, snapshot in load_recorded_snapshots().items():
        ltm, bridge, eps, multiples = value_snapshot(snapshot, today=TODAY)
        v = value_company(snapshot, today=TODAY)
        assert (v.ltm, v.bridge, v.diluted_eps, v.multiples) == (ltm, bridge, eps.value, multiples), ticker


def test_value_company_merges_and_dedupes_flags():
    snapshot = hd_like(
        "X",
        diluted_shares=None,
        basic_shares=1_000e6,
        balance_sheet=HOME_DEPOT.balance_sheet.model_copy(update={"total_debt": None}),
        quarters=[q.model_copy(update={"nci_income": None}) for q in HOME_DEPOT.quarters],
    )
    v = value_company(snapshot, today=TODAY)
    codes = [f.code for f in v.flags]
    assert FlagCode.BASIC_SHARES_FALLBACK in codes      # from the bridge
    assert FlagCode.MISSING_TOTAL_DEBT in codes         # from the bridge
    assert FlagCode.EPS_FROM_CONSOLIDATED_NI in codes   # from the EPS calculation
    assert len(codes) == len(set((f.code, f.message) for f in v.flags))


def test_value_company_flags_stale_filing_relative_to_today():
    v = value_company(HOME_DEPOT, today=date(2027, 6, 1))
    assert FlagCode.STALE_FILING in [f.code for f in v.flags]


def test_value_company_needs_four_quarters():
    with pytest.raises(InsufficientDataError) as exc_info:
        value_company(hd_like("X", quarters=HOME_DEPOT.quarters[:3]), today=TODAY)
    assert exc_info.value.ticker == "X"
    assert "quarter" in str(exc_info.value)


def test_value_company_needs_a_share_count():
    with pytest.raises(InsufficientDataError):
        value_company(hd_like("X", diluted_shares=None, basic_shares=None), today=TODAY)


# --- run_comps -------------------------------------------------------------


@pytest.fixture
def provider():
    return CountingProvider(
        {
            "HD": HOME_DEPOT,
            "LOW": hd_like("LOW", share_price=200.0),
            "FND": hd_like("FND", share_price=400.0),
            "CAD": hd_like("CAD", price_currency="CAD", reporting_currency="CAD"),
            "THIN": hd_like("THIN", quarters=HOME_DEPOT.quarters[:2]),
        }
    )


def test_run_comps_happy_path(provider):
    result = run_comps(provider, target="HD", peers=["LOW", "FND"], today=TODAY)
    assert result.as_of == TODAY
    assert result.target.ticker == "HD"
    assert [p.ticker for p in result.peers] == ["LOW", "FND"]
    assert result.errors == {}
    assert result.statistics.ev_ebitda.n == 2
    assert result.statistics.pe.n == 2
    assert result.implied.mid_basis is MidBasis.MEDIAN
    assert result.implied.ev_ebitda.kind is MultipleKind.EV_EBITDA
    assert result.implied.ev_ebitda.n == 2
    assert not result.implied.ev_ebitda.is_nm
    # Peers are HD at other prices, so the peer median EV/EBITDA bracket
    # (200 and 400 vs 321.05) brackets HD's own price.
    assert result.implied.ev_ebitda.low < 321.05 < result.implied.ev_ebitda.high


def test_run_comps_bad_peer_lands_in_errors(provider):
    result = run_comps(provider, target="HD", peers=["LOW", "ZZZZNOPE", "FND"], today=TODAY)
    assert [p.ticker for p in result.peers] == ["LOW", "FND"]
    assert set(result.errors) == {"ZZZZNOPE"}
    err = result.errors["ZZZZNOPE"]
    assert err.ticker == "ZZZZNOPE"
    assert err.code is ErrorCode.TICKER_NOT_FOUND
    assert "ZZZZNOPE" in err.message
    assert result.statistics.ev_revenue.n == 2


def test_run_comps_currency_mismatch_and_thin_peers_are_errors(provider):
    result = run_comps(provider, target="HD", peers=["CAD", "THIN", "LOW"], today=TODAY)
    assert [p.ticker for p in result.peers] == ["LOW"]
    assert result.errors["CAD"].code is ErrorCode.CURRENCY_MISMATCH
    assert result.errors["THIN"].code is ErrorCode.INSUFFICIENT_DATA
    assert result.statistics.ev_ebitda.n == 1


def test_run_comps_peer_source_failure_lands_in_errors(provider):
    class Flaky(CountingProvider):
        def fetch_company(self, ticker):
            if ticker == "LOW":
                raise MarketDataError("LOW: yfinance request failed: timeout")
            return super().fetch_company(ticker)

    flaky = Flaky(provider.snapshots)
    result = run_comps(flaky, target="HD", peers=["LOW", "FND"], today=TODAY)
    assert [p.ticker for p in result.peers] == ["FND"]
    assert result.errors["LOW"].code is ErrorCode.SOURCE_ERROR
    assert "timeout" in result.errors["LOW"].message


def test_run_comps_normalises_dedupes_and_drops_target_from_peers(provider):
    result = run_comps(provider, target=" hd ", peers=["low", "LOW", "HD", " fnd"], today=TODAY)
    assert result.target.ticker == "HD"
    assert [p.ticker for p in result.peers] == ["LOW", "FND"]
    assert result.errors == {}


def test_run_comps_with_no_usable_peers_is_all_nm(provider):
    result = run_comps(provider, target="HD", peers=["ZZZZNOPE"], today=TODAY)
    assert result.peers == []
    assert result.statistics.ev_ebitda.n == 0
    assert result.implied.ev_ebitda.is_nm
    assert result.implied.pe.is_nm


def test_run_comps_target_failure_raises(provider):
    with pytest.raises(TickerNotFoundError):
        run_comps(provider, target="ZZZZNOPE", peers=["LOW"], today=TODAY)
    with pytest.raises(InsufficientDataError):
        run_comps(provider, target="THIN", peers=["LOW"], today=TODAY)


def test_run_comps_mean_basis(provider):
    median = run_comps(provider, target="HD", peers=["LOW", "FND", "FND"], today=TODAY)
    mean = run_comps(provider, target="HD", peers=["LOW", "FND"], mid_basis=MidBasis.MEAN, today=TODAY)
    assert mean.implied.mid_basis is MidBasis.MEAN
    assert mean.implied.ev_ebitda.mid == pytest.approx(median.implied.ev_ebitda.mid)  # two peers: mean == median
    stats = mean.statistics.ev_ebitda
    assert stats.mean == pytest.approx(stats.median)


def test_run_comps_uses_batch_fetch_for_peers(provider):
    run_comps(provider, target="HD", peers=["LOW", "FND"], today=TODAY)
    assert provider.batch_calls == [["LOW", "FND"]]


def test_run_comps_on_recorded_real_snapshots():
    fixture = FixtureProvider(load_recorded_snapshots())
    result = run_comps(fixture, target="HD", peers=["LOW", "FND", "TSCO", "WSM"], today=TODAY)
    assert result.statistics.ev_ebitda.n == 4
    assert result.target.multiples.ev_ebitda.value == pytest.approx(14.628, abs=5e-4)


# --- suggest_peers ---------------------------------------------------------
#
# Without a proxy source the suggestion is the industry screen alone:
# same industry, inside the market-cap band, closest in size first. The
# sector is not a fallback (it puts restaurants next to a home-improvement
# retailer); a thin screen is returned as found and flagged. The proxy
# step and the union are covered in test_peer_resolution.py.


def profile(ticker, cap, *, industry="Home Improvement Retail", sector="Consumer Cyclical", **kw):
    return CompanyProfile(
        ticker=ticker, name=ticker, sector=sector, industry=industry, market_cap=cap,
        price_currency=kw.get("price_currency", "USD"), reporting_currency=kw.get("reporting_currency", "USD"),
    )


class ScreenCounter(FixtureProvider):
    def __init__(self, profiles):
        super().__init__(profiles=profiles)
        self.screens = []

    def screen_peers(self, **kw):
        self.screens.append((kw["industry"], kw["sector"]))
        return super().screen_peers(**kw)


def tagged(result):
    return [(p.ticker, p.sources) for p in result.peers]


def test_suggest_peers_screens_the_industry_inside_the_band():
    provider = ScreenCounter(
        {
            "HD": profile("HD", 320 * B),
            "LOW": profile("LOW", 115 * B),
            "FND": profile("FND", 5 * B),                                   # in industry, below band
            "TSCO": profile("TSCO", 150 * B, industry="Specialty Retail"),  # sector only: not a candidate
            "MCD": profile("MCD", 220 * B, industry="Restaurants"),
            "XOM": profile("XOM", 300 * B, industry="Oil & Gas Integrated", sector="Energy"),
        }
    )
    result = suggest_peers(provider, "hd")
    assert result.target.ticker == "HD"
    assert result.market_cap_band.low == pytest.approx(0.2 * 320 * B)
    assert result.market_cap_band.high == pytest.approx(5.0 * 320 * B)
    assert tagged(result) == [("LOW", [PeerSource.SCREEN])]
    assert result.proxy is None and result.proxy_label is None and result.proxy_dropped == []
    assert result.screen_label.startswith("Companies Yahoo files under Home Improvement Retail with a market cap 0.2x–5.0x HD's")
    assert [f.code for f in result.flags] == [FlagCode.THIN_PEER_SET]
    assert "Only 1 Home Improvement Retail name within 0.2x-5.0x of HD's market cap" in result.flags[0].message
    assert provider.screens == [("Home Improvement Retail", "Consumer Cyclical")]


def test_suggest_peers_orders_the_screen_by_size_similarity():
    caps = {  # ratio to HD's 320B, all in the 0.2x-5.0x band
        "S1": 330, "S2": 300, "S3": 400, "S4": 250, "S5": 500, "S6": 200, "S7": 700, "S8": 150, "S9": 900, "S10": 110,
    }
    provider = ScreenCounter({"HD": profile("HD", 320 * B), **{t: profile(t, cap * B) for t, cap in caps.items()}})
    result = suggest_peers(provider, "HD")
    # |log(cap / 320B)|: S1 .03, S2 .06, S3 .22, S4 .25, S5 .45, S6 .47, S8 .76, S7 .78, S9 1.0, S10 1.1
    assert [p.ticker for p in result.peers] == ["S1", "S2", "S3", "S4", "S5", "S6", "S8", "S7", "S9", "S10"]
    assert all(p.sources == [PeerSource.SCREEN] for p in result.peers)
    assert result.flags == []


def test_suggest_peers_never_widens_to_the_sector():
    provider = ScreenCounter(
        {"HD": profile("HD", 320 * B), "LOW": profile("LOW", 115 * B),
         "MCD": profile("MCD", 220 * B, industry="Restaurants"), "TSCO": profile("TSCO", 150 * B, industry="Specialty Retail")}
    )
    result = suggest_peers(provider, "HD")
    assert [p.ticker for p in result.peers] == ["LOW"]
    assert [f.code for f in result.flags] == [FlagCode.THIN_PEER_SET]
    assert provider.screens == [("Home Improvement Retail", "Consumer Cyclical")]


def test_suggest_peers_with_four_industry_names_is_not_flagged():
    provider = ScreenCounter({"HD": profile("HD", 320 * B), **{t: profile(t, 150 * B) for t in ("A", "B", "C", "D")}})
    result = suggest_peers(provider, "HD")
    assert {p.ticker for p in result.peers} == {"A", "B", "C", "D"}
    assert result.flags == []


def test_suggest_peers_screens_the_sector_when_the_target_has_no_industry():
    provider = ScreenCounter(
        {"HD": profile("HD", 320 * B, industry=None), "MCD": profile("MCD", 220 * B, industry="Restaurants")}
    )
    result = suggest_peers(provider, "HD")
    assert tagged(result) == [("MCD", [PeerSource.SCREEN])]
    assert [f.code for f in result.flags] == [FlagCode.SCREEN_ON_SECTOR, FlagCode.THIN_PEER_SET]
    assert "HD has no industry classification, so the Consumer Cyclical sector was screened instead" in result.flags[0].message
    assert result.screen_label.startswith("HD has no industry classification, so the Consumer Cyclical sector was screened instead")
    assert "Only 1 name within" in result.flags[1].message
    assert provider.screens == [(None, "Consumer Cyclical")]


def test_suggest_peers_respects_limit():
    provider = ScreenCounter({"HD": profile("HD", 320 * B), **{f"P{i}": profile(f"P{i}", (150 + i) * B) for i in range(10)}})
    assert len(suggest_peers(provider, "HD", limit=4).peers) == 4


def test_suggest_peers_errors():
    with pytest.raises(TickerNotFoundError):
        suggest_peers(ScreenCounter({}), "HD")
    with pytest.raises(InsufficientDataError):
        suggest_peers(ScreenCounter({"HD": profile("HD", None)}), "HD")
    with pytest.raises(InsufficientDataError):
        suggest_peers(ScreenCounter({"HD": profile("HD", 320 * B, industry=None, sector=None)}), "HD")
    with pytest.raises(PeerDiscoveryUnavailableError):
        suggest_peers(FixtureProvider(), "HD")
