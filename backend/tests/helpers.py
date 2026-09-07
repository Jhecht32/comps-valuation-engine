"""Shared test helpers.

value_snapshot runs a CompanySnapshot through the engine end to end and
returns the intermediate objects (LTM, bridge, EPS, multiples) so
provider tests can assert on each step. The canonical composition the
API uses is app.services.comps.value_company; test_services.py checks
the two agree so this copy cannot drift.
"""

import json
from datetime import date
from pathlib import Path

from app.data.fixtures import FixtureProvider
from app.data.provider import (
    CompanyProfile,
    MarketDataError,
    MarketDataProvider,
    Quote,
    TickerNotFoundError,
)
from app.data.yfinance_provider import map_profile
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


# --- Recorded real data ------------------------------------------------------
#
# tests/fixtures/ holds CompanySnapshots and Ticker.info subsets pulled
# from Yahoo Finance through the yfinance provider on 2026-09-06 (prices
# as of the 2026-09-04 close). They let the API smoke test run a real
# comps request offline and deterministically.

FIXTURES_DIR = Path(__file__).parent / "fixtures"
RECORDED_AT = date(2026, 9, 6)


def load_recorded_snapshots() -> dict[str, CompanySnapshot]:
    return {
        path.stem: CompanySnapshot.model_validate_json(path.read_text())
        for path in sorted((FIXTURES_DIR / "snapshots").glob("*.json"))
    }


def load_recorded_profiles() -> dict[str, CompanyProfile]:
    profiles = {}
    for path in sorted((FIXTURES_DIR / "profiles").glob("*.json")):
        raw = json.loads(path.read_text())
        profiles[path.stem] = map_profile(raw["ticker"], raw)
    return profiles


def recorded_provider(**extra_profiles: CompanyProfile) -> FixtureProvider:
    profiles = load_recorded_profiles()
    profiles.update(extra_profiles)
    return FixtureProvider(load_recorded_snapshots(), profiles=profiles)
