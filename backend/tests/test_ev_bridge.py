"""Tests for the enterprise value bridge.

All monetary fixture values are in raw dollars (millions written out) so the
module never has to guess units.
"""

import pytest

from app.valuation.ev_bridge import build_ev_bridge
from app.valuation.models import FlagCode

M = 1_000_000


def test_full_bridge_with_all_components():
    bridge = build_ev_bridge(
        share_price=50.0,
        diluted_shares=100 * M,
        total_debt=1_200 * M,
        preferred_equity=100 * M,
        noncontrolling_interest=50 * M,
        cash_and_equivalents=800 * M,
        short_term_investments=200 * M,
    )
    assert bridge.equity_value == pytest.approx(5_000 * M)
    # 5,000 + 1,200 + 100 + 50 - 800 - 200 = 5,350
    assert bridge.enterprise_value == pytest.approx(5_350 * M)
    assert bridge.flags == []
    assert bridge.shares_are_basic_fallback is False


def test_missing_diluted_falls_back_to_basic_and_flags():
    bridge = build_ev_bridge(
        share_price=10.0,
        diluted_shares=None,
        basic_shares=90 * M,
        total_debt=0.0,
        cash_and_equivalents=0.0,
    )
    assert bridge.equity_value == pytest.approx(900 * M)
    assert bridge.shares_are_basic_fallback is True
    assert FlagCode.BASIC_SHARES_FALLBACK in [f.code for f in bridge.flags]


def test_zero_diluted_treated_as_unavailable():
    bridge = build_ev_bridge(
        share_price=10.0,
        diluted_shares=0,
        basic_shares=90 * M,
        total_debt=0.0,
        cash_and_equivalents=0.0,
    )
    assert bridge.shares_are_basic_fallback is True


def test_no_share_count_at_all_raises():
    with pytest.raises(ValueError):
        build_ev_bridge(share_price=10.0, diluted_shares=None, basic_shares=None)


def test_non_positive_price_raises():
    with pytest.raises(ValueError):
        build_ev_bridge(share_price=0.0, diluted_shares=100 * M)


def test_absent_preferred_nci_st_investments_default_to_zero_without_flags():
    # Most companies genuinely have none of these; absence is not an
    # approximation and must not pollute the data quality panel.
    bridge = build_ev_bridge(
        share_price=20.0,
        diluted_shares=10 * M,
        total_debt=50 * M,
        cash_and_equivalents=30 * M,
    )
    assert bridge.enterprise_value == pytest.approx(200 * M + 50 * M - 30 * M)
    assert bridge.flags == []


def test_missing_debt_and_cash_default_to_zero_but_are_flagged():
    # Every real company reports debt and cash lines; a missing value is a
    # data problem the user must see.
    bridge = build_ev_bridge(share_price=20.0, diluted_shares=10 * M)
    assert bridge.enterprise_value == pytest.approx(200 * M)
    codes = [f.code for f in bridge.flags]
    assert FlagCode.MISSING_TOTAL_DEBT in codes
    assert FlagCode.MISSING_CASH in codes


def test_absent_and_zero_preferred_nci_st_are_distinguished_in_the_model():
    # Same EV, same (empty) flags either way - but the model must not
    # collapse "absent from source" into "reported as zero".
    absent = build_ev_bridge(
        share_price=20.0,
        diluted_shares=10 * M,
        total_debt=50 * M,
        cash_and_equivalents=30 * M,
    )
    present_zero = build_ev_bridge(
        share_price=20.0,
        diluted_shares=10 * M,
        total_debt=50 * M,
        cash_and_equivalents=30 * M,
        preferred_equity=0.0,
        noncontrolling_interest=0.0,
        short_term_investments=0.0,
    )
    assert absent.enterprise_value == present_zero.enterprise_value
    assert absent.flags == present_zero.flags == []
    assert absent.preferred_equity == present_zero.preferred_equity == 0.0

    assert absent.preferred_equity_reported is False
    assert absent.noncontrolling_interest_reported is False
    assert absent.short_term_investments_reported is False
    assert present_zero.preferred_equity_reported is True
    assert present_zero.noncontrolling_interest_reported is True
    assert present_zero.short_term_investments_reported is True


def test_negative_enterprise_value_is_allowed():
    # Cash-rich company: EV below zero is unusual but legitimate output;
    # suppression happens at the multiples layer, not here.
    bridge = build_ev_bridge(
        share_price=5.0,
        diluted_shares=10 * M,
        total_debt=0.0,
        cash_and_equivalents=100 * M,
    )
    assert bridge.enterprise_value == pytest.approx(50 * M - 100 * M)
