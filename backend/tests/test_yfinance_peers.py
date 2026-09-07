"""Tests for the yfinance side of peer discovery: mapping Ticker.info to
a CompanyProfile, mapping screener quotes, and building the screener
query. The screener call is injected so nothing here touches Yahoo.

Verified shapes (yfinance 1.7.0, 2026-09-06): Ticker.info carries
sector / industry / marketCap / currency / financialCurrency; yf.screen
returns {"quotes": [...]} where each quote has symbol, longName,
marketCap, currency, financialCurrency, exchange, quoteType but NOT
sector or industry, so those are stamped from the query.
"""

import pytest

from app.data.provider import MarketDataError, TickerNotFoundError
from app.data.yfinance_provider import YFinanceProvider, map_profile, map_screen_quote

HD_INFO = {
    "longName": "The Home Depot, Inc.",
    "shortName": "Home Depot, Inc. (The)",
    "sector": "Consumer Cyclical",
    "industry": "Home Improvement Retail",
    "marketCap": 320308248576,
    "currency": "USD",
    "financialCurrency": "USD",
    "exchange": "NYQ",
    "quoteType": "EQUITY",
}

LOW_QUOTE = {
    "symbol": "LOW",
    "longName": "Lowe's Companies, Inc.",
    "shortName": "Lowe's Companies, Inc.",
    "marketCap": 114707488768,
    "currency": "USD",
    "financialCurrency": "USD",
    "exchange": "NYQ",
    "quoteType": "EQUITY",
}

WFAFF_QUOTE = {  # Wesfarmers foreign ordinary on the pink sheets
    "symbol": "WFAFF",
    "shortName": "Wesfarmers Ltd.",
    "marketCap": 66993029120,
    "currency": "USD",
    "financialCurrency": "AUD",
    "exchange": "PNK",
    "quoteType": "EQUITY",
}


# --- map_profile --------------------------------------------------------


def test_map_profile_reads_identity_sector_and_cap():
    p = map_profile("hd", HD_INFO)
    assert p.ticker == "HD"
    assert p.name == "The Home Depot, Inc."
    assert p.sector == "Consumer Cyclical"
    assert p.industry == "Home Improvement Retail"
    assert p.market_cap == 320308248576.0
    assert p.price_currency == "USD"
    assert p.reporting_currency == "USD"
    assert p.exchange == "NYQ"


def test_map_profile_tolerates_missing_fields():
    p = map_profile("X", {"shortName": "X Corp"})
    assert p.name == "X Corp"
    assert p.sector is None and p.industry is None and p.market_cap is None


# --- map_screen_quote ---------------------------------------------------


def test_map_screen_quote_stamps_sector_and_industry_from_query():
    p = map_screen_quote(LOW_QUOTE, sector="Consumer Cyclical", industry="Home Improvement Retail")
    assert p.ticker == "LOW"
    assert p.name == "Lowe's Companies, Inc."
    assert p.industry == "Home Improvement Retail"
    assert p.sector == "Consumer Cyclical"
    assert p.market_cap == 114707488768.0
    assert p.reporting_currency == "USD"


@pytest.mark.parametrize("cap", ["N/A", "", None, float("nan")])
def test_map_screen_quote_tolerates_unusable_market_cap(cap):
    p = map_screen_quote({**LOW_QUOTE, "marketCap": cap}, sector="Consumer Cyclical", industry=None)
    assert p.market_cap is None


def test_map_screen_quote_keeps_foreign_reporting_currency():
    p = map_screen_quote(WFAFF_QUOTE, sector="Consumer Cyclical", industry="Home Improvement Retail")
    assert p.price_currency == "USD"
    assert p.reporting_currency == "AUD"  # select_peers drops it; the mapper only reports


# --- Provider: get_profile ----------------------------------------------


class FakeTicker:
    def __init__(self, info):
        self._info = info

    @property
    def info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


def test_get_profile_maps_info():
    provider = YFinanceProvider(ticker_factory=lambda s: FakeTicker(HD_INFO))
    assert provider.get_profile("HD").industry == "Home Improvement Retail"


@pytest.mark.parametrize("info", [{}, None, {"trailingPegRatio": None}, {"quoteType": "NONE", "symbol": "ZZZZ"}])
def test_get_profile_unknown_ticker(info):
    provider = YFinanceProvider(ticker_factory=lambda s: FakeTicker(info))
    with pytest.raises(TickerNotFoundError):
        provider.get_profile("ZZZZNOPE")


def test_get_profile_wraps_transport_errors():
    provider = YFinanceProvider(ticker_factory=lambda s: FakeTicker(ConnectionError("boom")))
    with pytest.raises(MarketDataError):
        provider.get_profile("HD")


# --- Provider: screen_peers ---------------------------------------------


class FakeScreen:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_screen_peers_builds_industry_query_with_band_and_maps_quotes():
    screen = FakeScreen({"quotes": [LOW_QUOTE, WFAFF_QUOTE, {**LOW_QUOTE, "symbol": "SPY", "quoteType": "ETF"}]})
    provider = YFinanceProvider(screen_fn=screen)
    found = provider.screen_peers(
        sector="Consumer Cyclical", industry="Home Improvement Retail",
        market_cap_min=80e9, market_cap_max=1280e9,
    )
    assert [p.ticker for p in found] == ["LOW", "WFAFF"]  # ETF dropped
    assert all(p.industry == "Home Improvement Retail" for p in found)

    (query, kwargs), = screen.calls
    d = query.to_dict()
    assert d["operator"] == "AND"
    clauses = {op["operands"][0]: op for op in d["operands"] if op["operator"] != "OR"}
    assert clauses["region"]["operands"] == ["region", "us"]
    assert clauses["industry"]["operands"] == ["industry", "Home Improvement Retail"]
    assert clauses["intradaymarketcap"]["operands"] == ["intradaymarketcap", 80e9, 1280e9]
    assert "sector" not in clauses
    assert kwargs["size"] <= 250


def test_screen_peers_restricts_to_primary_us_exchanges():
    # Region 'us' alone admits pink-sheet foreign ordinaries (Wesfarmers,
    # Kingfisher, Prosus); the exchange clause keeps NYSE/Nasdaq listings.
    screen = FakeScreen({"quotes": []})
    provider = YFinanceProvider(screen_fn=screen)
    provider.screen_peers(sector="Consumer Cyclical", industry=None, market_cap_min=1.0, market_cap_max=2.0)
    (query, _), = screen.calls
    # EquityQuery renders is-in as an OR of EQ clauses.
    exchange_clause = next(op for op in query.to_dict()["operands"] if op["operator"] == "OR")
    assert all(op["operands"][0] == "exchange" for op in exchange_clause["operands"])
    exchanges = {op["operands"][1] for op in exchange_clause["operands"]}
    assert {"NYQ", "NMS", "NGM", "NCM", "ASE"} <= exchanges
    assert not {"PNK", "OQX", "OQB"} & exchanges


def test_screen_peers_falls_back_to_sector_query():
    screen = FakeScreen({"quotes": []})
    provider = YFinanceProvider(screen_fn=screen)
    provider.screen_peers(sector="Consumer Cyclical", industry=None, market_cap_min=1.0, market_cap_max=2.0)
    (query, _), = screen.calls
    ops = [op["operands"] for op in query.to_dict()["operands"]]
    assert ["sector", "Consumer Cyclical"] in ops
    assert not any(o[0] == "industry" for o in ops)


def test_screen_peers_requires_sector_or_industry():
    provider = YFinanceProvider(screen_fn=FakeScreen({"quotes": []}))
    with pytest.raises(ValueError):
        provider.screen_peers(sector=None, industry=None, market_cap_min=1.0, market_cap_max=2.0)


def test_screen_peers_rejects_unknown_industry_as_market_data_error():
    # EquityQuery validates industry names against Yahoo's list; a name it
    # does not know must surface as a source problem, not a 500.
    provider = YFinanceProvider(screen_fn=FakeScreen({"quotes": []}))
    with pytest.raises(MarketDataError):
        provider.screen_peers(sector="Consumer Cyclical", industry="Not A Yahoo Industry",
                              market_cap_min=1.0, market_cap_max=2.0)


def test_screen_peers_wraps_transport_errors():
    provider = YFinanceProvider(screen_fn=FakeScreen(ConnectionError("boom")))
    with pytest.raises(MarketDataError):
        provider.screen_peers(sector="Consumer Cyclical", industry=None, market_cap_min=1.0, market_cap_max=2.0)
