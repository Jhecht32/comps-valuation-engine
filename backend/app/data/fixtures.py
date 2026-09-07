"""Fixture provider: canned snapshots for tests and offline development.

The default Home Depot snapshot is derived from the golden hand model in
tests/test_home_depot_golden.py so that FixtureProvider -> engine
reproduces the golden multiples exactly. The golden model works from
half-year YTD figures, which the snapshot cannot carry, so each half is
split evenly into two synthetic quarters: the individual quarters are
NOT Home Depot's reported quarterly results, only their sums are. Once
the yfinance provider is verified, recorded real snapshots can replace
this one.
"""

from datetime import date
from typing import Mapping

from app.data.provider import (
    CompanyProfile,
    MarketDataProvider,
    PeerDiscoveryUnavailableError,
    TickerNotFoundError,
)
from app.valuation.models import (
    AnnualFinancials,
    BalanceSheetItems,
    CompanySnapshot,
    QuarterlyFinancials,
)

_M = 1_000_000.0


def _hd_quarter(period_end: date, revenue: float, ebit: float, net_income: float, d_and_a: float):
    return QuarterlyFinancials(
        period_end=period_end,
        revenue=revenue * _M,
        gross_profit=None,  # not part of the golden model
        ebit=ebit * _M,
        net_income=net_income * _M,
        d_and_a=d_and_a * _M,
        preferred_dividends=0.0,  # known-zero: no preferred stock
        nci_income=0.0,  # known-zero: no noncontrolling interests
    )


# Golden inputs ($mm): H1 FY2026 YTD (six months ended 2026-08-02) and
# H2 FY2025 = FY2025 - H1 FY2025 YTD. Each half is split evenly below.
_H1_FY26 = dict(revenue=89_626, ebit=11_820, net_income=8_055, d_and_a=2_188)
_H2_FY25 = dict(
    revenue=164_683 - 85_133,
    ebit=20_890 - 11_688,
    net_income=14_156 - 7_984,
    d_and_a=4_121 - 1_998,
)


def _half(figures: dict[str, float]) -> dict[str, float]:
    return {k: v / 2 for k, v in figures.items()}


HOME_DEPOT = CompanySnapshot(
    ticker="HD",
    name="The Home Depot, Inc.",
    price_currency="USD",
    reporting_currency="USD",
    share_price=321.05,  # close of 2026-09-04
    price_as_of=date(2026, 9, 4),
    diluted_shares=996 * _M,
    basic_shares=None,
    shares_as_of=date(2026, 8, 2),
    balance_sheet=BalanceSheetItems(
        as_of=date(2026, 8, 2),
        total_debt=(4_248 + 4_697 + 43_951) * _M,  # ST debt + current LTD + LTD
        cash_and_equivalents=2_085 * _M,
        short_term_investments=None,  # absent from the balance sheet
        preferred_equity=0.0,  # present, zero
        noncontrolling_interest=0.0,  # present, zero
    ),
    quarters=[
        _hd_quarter(date(2026, 8, 2), **_half(_H1_FY26)),  # Q2 FY2026 (synthetic split)
        _hd_quarter(date(2026, 5, 3), **_half(_H1_FY26)),  # Q1 FY2026 (synthetic split)
        _hd_quarter(date(2026, 2, 1), **_half(_H2_FY25)),  # Q4 FY2025 (synthetic split)
        _hd_quarter(date(2025, 11, 2), **_half(_H2_FY25)),  # Q3 FY2025 (synthetic split)
    ],
    fiscal_years=[
        AnnualFinancials(  # 10-K, 52 weeks ended 2026-02-01
            period_end=date(2026, 2, 1),
            revenue=164_683 * _M,
            gross_profit=None,
            ebit=20_890 * _M,
            net_income=14_156 * _M,
            d_and_a=4_121 * _M,
            preferred_dividends=0.0,
            nci_income=0.0,
        ),
    ],
    source="fixture",
)

DEFAULT_FIXTURES: dict[str, CompanySnapshot] = {"HD": HOME_DEPOT}


class FixtureProvider(MarketDataProvider):
    """Serves canned snapshots and, when given, canned profiles. Without
    profiles, peer discovery reports itself unavailable rather than
    returning an empty universe, so a misconfigured test fails loudly."""

    def __init__(
        self,
        snapshots: Mapping[str, CompanySnapshot] | None = None,
        *,
        profiles: Mapping[str, CompanyProfile] | None = None,
    ):
        self._snapshots = dict(DEFAULT_FIXTURES if snapshots is None else snapshots)
        self._profiles = None if profiles is None else dict(profiles)

    def fetch_company(self, ticker: str) -> CompanySnapshot:
        key = ticker.strip().upper()
        try:
            snapshot = self._snapshots[key]
        except KeyError:
            raise TickerNotFoundError(ticker.strip()) from None
        return snapshot.model_copy(deep=True)

    def get_profile(self, ticker: str) -> CompanyProfile:
        if self._profiles is None:
            raise PeerDiscoveryUnavailableError(ticker)
        key = ticker.strip().upper()
        try:
            return self._profiles[key].model_copy()
        except KeyError:
            raise TickerNotFoundError(ticker.strip()) from None

    def screen_peers(
        self,
        *,
        sector: str | None,
        industry: str | None,
        market_cap_min: float,
        market_cap_max: float,
        limit: int = 50,
    ) -> list[CompanyProfile]:
        if self._profiles is None:
            raise PeerDiscoveryUnavailableError()
        if industry is not None:
            matches = (p for p in self._profiles.values() if p.industry == industry)
        elif sector is not None:
            matches = (p for p in self._profiles.values() if p.sector == sector)
        else:
            raise ValueError("screen_peers needs a sector or an industry")
        found = [
            p.model_copy()
            for p in matches
            if p.market_cap is not None and market_cap_min <= p.market_cap <= market_cap_max
        ]
        return found[:limit]
