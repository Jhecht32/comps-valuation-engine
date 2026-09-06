"""Shared test helpers.

value_snapshot runs a CompanySnapshot through the engine end to end. The
API layer will grow its own composition function for this; tests keep a
copy here so provider tests can assert on final multiples without
depending on that layer.
"""

from datetime import date

from app.data.provider import (
    MarketDataError,
    MarketDataProvider,
    Quote,
    TickerNotFoundError,
)
from app.valuation.ev_bridge import build_ev_bridge
from app.valuation.ltm import ltm_from_quarters
from app.valuation.models import CompanySnapshot
from app.valuation.multiples import compute_multiples, ltm_diluted_eps


def value_snapshot(snapshot: CompanySnapshot, *, today: date | None = None):
    ltm = ltm_from_quarters(snapshot.quarters, today=today)
    bs = snapshot.balance_sheet
    bridge = build_ev_bridge(
        share_price=snapshot.share_price,
        diluted_shares=snapshot.diluted_shares,
        basic_shares=snapshot.basic_shares,
        total_debt=bs.total_debt,
        preferred_equity=bs.preferred_equity,
        noncontrolling_interest=bs.noncontrolling_interest,
        cash_and_equivalents=bs.cash_and_equivalents,
        short_term_investments=bs.short_term_investments,
    )
    eps = ltm_diluted_eps(
        net_income=ltm.net_income,
        preferred_dividends=ltm.preferred_dividends,
        nci_income=ltm.nci_income,
        diluted_shares=bridge.diluted_shares,
    )
    multiples = compute_multiples(
        enterprise_value=bridge.enterprise_value,
        share_price=snapshot.share_price,
        ltm=ltm,
        diluted_eps=eps.value,
    )
    return ltm, bridge, eps, multiples


class CountingProvider(MarketDataProvider):
    """In-memory provider that records every call, for cache tests."""

    def __init__(self, snapshots: dict[str, CompanySnapshot]):
        self.snapshots = dict(snapshots)
        self.quotes: dict[str, Quote] = {}
        self.fetch_calls: list[str] = []
        self.quote_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def fetch_company(self, ticker: str) -> CompanySnapshot:
        self.fetch_calls.append(ticker)
        try:
            return self.snapshots[ticker].model_copy(deep=True)
        except KeyError:
            raise TickerNotFoundError(ticker) from None

    def get_quote(self, ticker: str) -> Quote:
        self.quote_calls.append(ticker)
        if ticker in self.quotes:
            return self.quotes[ticker]
        return super().get_quote(ticker)

    def get_companies(self, tickers: list[str]) -> dict[str, CompanySnapshot | MarketDataError]:
        self.batch_calls.append(list(tickers))
        return super().get_companies(tickers)
