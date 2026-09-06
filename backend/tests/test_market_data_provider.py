"""Tests for the MarketDataProvider interface and the fixture stub.

The fixture provider returns a Home Depot snapshot derived from the
golden hand model, so running it through the engine must reproduce the
golden figures exactly. That makes the stub a network-free stand-in for
the whole pipeline and pins the snapshot contract: raw dollars, newest
period first, None for absent lines, 0.0 for known-zero lines.
"""

from datetime import date

import pytest

from app.data import FixtureProvider, MarketDataProvider, TickerNotFoundError
from app.valuation.models import (
    AnnualFinancials,
    BalanceSheetItems,
    CompanySnapshot,
    FlagCode,
    LTMMethod,
    QuarterlyFinancials,
)
from tests.helpers import value_snapshot

M = 1_000_000


def test_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        MarketDataProvider()


def test_fixture_provider_is_a_market_data_provider():
    assert isinstance(FixtureProvider(), MarketDataProvider)


def test_unknown_ticker_raises_not_found():
    with pytest.raises(TickerNotFoundError) as excinfo:
        FixtureProvider().get_company("NOPE")
    assert excinfo.value.ticker == "NOPE"


def test_ticker_lookup_is_case_and_whitespace_insensitive():
    snapshot = FixtureProvider().get_company(" hd ")
    assert snapshot.ticker == "HD"


def test_hd_snapshot_shape():
    snapshot = FixtureProvider().get_company("HD")
    assert isinstance(snapshot, CompanySnapshot)
    assert snapshot.source == "fixture"
    assert snapshot.price_currency == "USD"
    assert snapshot.reporting_currency == "USD"
    assert snapshot.share_price == 321.05
    assert snapshot.price_as_of == date(2026, 9, 4)
    assert snapshot.diluted_shares == 996 * M
    assert isinstance(snapshot.balance_sheet, BalanceSheetItems)
    assert snapshot.balance_sheet.as_of == date(2026, 8, 2)
    assert all(isinstance(q, QuarterlyFinancials) for q in snapshot.quarters)
    assert all(isinstance(fy, AnnualFinancials) for fy in snapshot.fiscal_years)


def test_periods_are_newest_first():
    snapshot = FixtureProvider().get_company("HD")
    ends = [q.period_end for q in snapshot.quarters]
    assert ends == sorted(ends, reverse=True)
    assert ends[0] == date(2026, 8, 2)
    fy_ends = [fy.period_end for fy in snapshot.fiscal_years]
    assert fy_ends == sorted(fy_ends, reverse=True)
    assert fy_ends[0] == date(2026, 2, 1)


def test_absent_vs_known_zero_survives_the_bridge():
    snapshot = FixtureProvider().get_company("HD")
    bs = snapshot.balance_sheet
    assert bs.short_term_investments is None  # absent from HD's balance sheet
    assert bs.preferred_equity == 0.0  # known-zero
    assert bs.noncontrolling_interest == 0.0

    _, bridge, _, _ = value_snapshot(snapshot)
    assert bridge.short_term_investments_reported is False
    assert bridge.preferred_equity_reported is True
    assert bridge.noncontrolling_interest_reported is True


def test_known_zero_breakouts_keep_eps_unflagged():
    snapshot = FixtureProvider().get_company("HD")
    assert all(q.preferred_dividends == 0.0 for q in snapshot.quarters)
    assert all(q.nci_income == 0.0 for q in snapshot.quarters)
    _, _, eps, _ = value_snapshot(snapshot)
    assert eps.flags == []


def test_fixture_hd_ties_to_golden_model():
    snapshot = FixtureProvider().get_company("HD")
    ltm, bridge, eps, multiples = value_snapshot(snapshot, today=date(2026, 9, 4))

    assert ltm.method == LTMMethod.QUARTERLY
    assert ltm.as_of == date(2026, 8, 2)
    assert ltm.revenue == pytest.approx(169_176 * M)
    assert ltm.ebit == pytest.approx(21_022 * M)
    assert ltm.net_income == pytest.approx(14_227 * M)
    assert ltm.ebitda == pytest.approx(25_333 * M)
    assert ltm.flags == []  # gross profit absent from the fixture, but it is informational

    assert bridge.equity_value == pytest.approx(319_765.8 * M)
    assert bridge.enterprise_value == pytest.approx(370_576.8 * M)
    assert bridge.flags == []

    assert round(eps.value, 3) == 14.284
    assert round(multiples.ev_revenue.value, 3) == 2.190
    assert round(multiples.ev_ebitda.value, 3) == 14.628
    assert round(multiples.ev_ebit.value, 3) == 17.628
    assert round(multiples.pe.value, 3) == 22.476


def test_fixture_provider_hands_out_copies():
    provider = FixtureProvider()
    first = provider.get_company("HD")
    first.quarters.clear()
    assert len(provider.get_company("HD").quarters) == 4


def test_custom_fixtures_replace_defaults():
    provider = FixtureProvider(snapshots={})
    with pytest.raises(TickerNotFoundError):
        provider.get_company("HD")
