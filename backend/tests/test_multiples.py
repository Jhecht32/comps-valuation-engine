"""Tests for trading multiples and NM (not meaningful) suppression.

NM rules: denominator negative, zero, or unavailable; EV/EBITDA above the
100x sanity cap. NM multiples carry a human-readable reason and are never
rendered as numbers.
"""

from datetime import date

import pytest

from app.valuation.models import FlagCode, LTMFinancials, LTMMethod
from app.valuation.multiples import compute_multiples, ltm_diluted_eps

M = 1_000_000


def make_ltm(**overrides):
    base = dict(
        revenue=1_000 * M,
        gross_profit=500 * M,
        ebitda=250 * M,
        ebit=200 * M,
        net_income=120 * M,
        as_of=date(2025, 6, 30),
        method=LTMMethod.QUARTERLY,
        flags=[],
    )
    base.update(overrides)
    return LTMFinancials(**base)


def test_clean_multiples():
    m = compute_multiples(
        enterprise_value=3_000 * M,
        share_price=60.0,
        ltm=make_ltm(),
        diluted_eps=4.0,
    )
    assert m.ev_revenue.value == pytest.approx(3.0)
    assert m.ev_ebitda.value == pytest.approx(12.0)
    assert m.ev_ebit.value == pytest.approx(15.0)
    assert m.pe.value == pytest.approx(15.0)
    assert not any(x.is_nm for x in (m.ev_revenue, m.ev_ebitda, m.ev_ebit, m.pe))


def test_negative_ebitda_is_nm_with_reason():
    m = compute_multiples(
        enterprise_value=3_000 * M,
        share_price=60.0,
        ltm=make_ltm(ebitda=-50 * M),
        diluted_eps=4.0,
    )
    assert m.ev_ebitda.is_nm
    assert m.ev_ebitda.value is None
    assert m.ev_ebitda.nm_reason  # non-empty explanation for the UI
    assert m.ev_revenue.value == pytest.approx(3.0)  # others unaffected


def test_zero_denominator_is_nm():
    m = compute_multiples(
        enterprise_value=3_000 * M,
        share_price=60.0,
        ltm=make_ltm(revenue=0.0),
        diluted_eps=4.0,
    )
    assert m.ev_revenue.is_nm


def test_unavailable_denominator_is_nm():
    m = compute_multiples(
        enterprise_value=3_000 * M,
        share_price=60.0,
        ltm=make_ltm(ebitda=None),
        diluted_eps=4.0,
    )
    assert m.ev_ebitda.is_nm


def test_ev_ebitda_above_100x_cap_is_nm():
    m = compute_multiples(
        enterprise_value=10_100 * M,
        share_price=60.0,
        ltm=make_ltm(ebitda=100 * M),
        diluted_eps=4.0,
    )
    assert m.ev_ebitda.is_nm
    assert "100" in m.ev_ebitda.nm_reason


def test_ev_ebitda_at_exactly_100x_is_kept():
    # Spec says "exceeds 100x": the boundary itself is still shown.
    m = compute_multiples(
        enterprise_value=10_000 * M,
        share_price=60.0,
        ltm=make_ltm(ebitda=100 * M),
        diluted_eps=4.0,
    )
    assert not m.ev_ebitda.is_nm
    assert m.ev_ebitda.value == pytest.approx(100.0)


def test_negative_eps_makes_pe_nm():
    m = compute_multiples(
        enterprise_value=3_000 * M,
        share_price=60.0,
        ltm=make_ltm(net_income=-10 * M),
        diluted_eps=-0.5,
    )
    assert m.pe.is_nm


def test_negative_ev_makes_all_ev_multiples_nm():
    # A negative EV multiple is mathematically defined but economically
    # meaningless and would corrupt the peer median.
    m = compute_multiples(
        enterprise_value=-100 * M,
        share_price=60.0,
        ltm=make_ltm(),
        diluted_eps=4.0,
    )
    for mult in (m.ev_revenue, m.ev_ebitda, m.ev_ebit):
        assert mult.is_nm
        assert mult.value is None
        assert "enterprise value" in mult.nm_reason.lower()
        assert "negative" in mult.nm_reason.lower()
    assert not m.pe.is_nm  # P/E has no EV numerator; unaffected


class TestLTMDilutedEPS:
    """EPS must use income attributable to common shareholders:
    net income - preferred dividends - income attributable to NCI.
    Otherwise P/E and the EV multiples measure different capital
    structures. None for a breakout means "not broken out by the source"
    (0.0 means known-zero) and triggers a flagged consolidated fallback.
    """

    def test_preferred_dividends_are_deducted(self):
        eps = ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=6 * M, nci_income=0.0, diluted_shares=30 * M
        )
        assert eps.value == pytest.approx(3.8)
        assert eps.flags == []

    def test_nci_income_is_deducted(self):
        eps = ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=0.0, nci_income=15 * M, diluted_shares=30 * M
        )
        assert eps.value == pytest.approx(3.5)
        assert eps.flags == []

    def test_both_deductions_together(self):
        eps = ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=6 * M, nci_income=15 * M, diluted_shares=30 * M
        )
        assert eps.value == pytest.approx(3.3)

    def test_missing_breakouts_fall_back_to_consolidated_and_flag(self):
        eps = ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=None, nci_income=None, diluted_shares=30 * M
        )
        assert eps.value == pytest.approx(4.0)
        assert FlagCode.EPS_FROM_CONSOLIDATED_NI in [f.code for f in eps.flags]

    def test_partial_breakout_subtracts_known_piece_and_still_flags(self):
        eps = ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=6 * M, nci_income=None, diluted_shares=30 * M
        )
        assert eps.value == pytest.approx(3.8)
        assert FlagCode.EPS_FROM_CONSOLIDATED_NI in [f.code for f in eps.flags]

    def test_unavailable_net_income_or_shares_yields_none(self):
        assert ltm_diluted_eps(
            net_income=None, preferred_dividends=0.0, nci_income=0.0, diluted_shares=30 * M
        ).value is None
        assert ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=0.0, nci_income=0.0, diluted_shares=None
        ).value is None
        assert ltm_diluted_eps(
            net_income=120 * M, preferred_dividends=0.0, nci_income=0.0, diluted_shares=0
        ).value is None


# --- EBITDA margin ---------------------------------------------------------


def test_ebitda_margin_is_ebitda_over_revenue_or_none():
    from datetime import date

    from app.valuation.models import LTMFinancials, LTMMethod
    from app.valuation.multiples import ebitda_margin

    def ltm(revenue, ebitda):
        return LTMFinancials(
            revenue=revenue, gross_profit=None, ebitda=ebitda, ebit=None, net_income=None,
            as_of=date(2026, 8, 2), method=LTMMethod.QUARTERLY, flags=[],
        )

    assert ebitda_margin(ltm(200.0, 30.0)) == 0.15
    assert ebitda_margin(ltm(200.0, -10.0)) == -0.05  # a loss-making margin is still a margin
    assert ebitda_margin(ltm(None, 30.0)) is None
    assert ebitda_margin(ltm(200.0, None)) is None
    assert ebitda_margin(ltm(0.0, 30.0)) is None and ebitda_margin(ltm(-5.0, 30.0)) is None
