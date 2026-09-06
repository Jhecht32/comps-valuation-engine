"""Tests for the implied valuation of the target (the bridge run in reverse).

    Implied EV        = peer multiple x target LTM metric
    Implied Equity    = Implied EV - total debt - preferred - NCI
                        + cash + short-term investments
    Implied Per Share = Implied Equity / target diluted shares

P/E skips the bridge: Implied Per Share = peer P/E x target LTM diluted EPS.

Short-term investments are added back so the reverse bridge is the exact
inverse of the forward bridge; the round-trip tests below enforce that.
"""

from datetime import date

import pytest

from app.valuation.ev_bridge import build_ev_bridge
from app.valuation.implied import implied_per_share, implied_range
from app.valuation.models import (
    LTMFinancials,
    LTMMethod,
    MidBasis,
    MultipleKind,
    MultipleStats,
)
from app.valuation.multiples import compute_multiples, ltm_diluted_eps

M = 1_000_000

TARGET_BRIDGE = build_ev_bridge(
    share_price=30.0,
    diluted_shares=100 * M,
    total_debt=400 * M,
    preferred_equity=0.0,
    noncontrolling_interest=0.0,
    cash_and_equivalents=150 * M,
    short_term_investments=50 * M,
)

TARGET_LTM = LTMFinancials(
    revenue=1_500 * M,
    gross_profit=750 * M,
    ebitda=200 * M,
    ebit=160 * M,
    net_income=100 * M,
    as_of=date(2025, 6, 30),
    method=LTMMethod.QUARTERLY,
    flags=[],
)


def make_stats(p25, median, mean, p75, n=5):
    return MultipleStats(n=n, min=p25, p25=p25, median=median, mean=mean, p75=p75, max=p75)


def test_ev_multiple_reverse_bridge_arithmetic():
    # 10x EBITDA of 200 -> EV 2,000; equity = 2,000 - 400 + 150 + 50 = 1,800
    per_share = implied_per_share(
        kind=MultipleKind.EV_EBITDA, multiple=10.0, bridge=TARGET_BRIDGE, ltm=TARGET_LTM
    )
    assert per_share == pytest.approx(18.0)


def test_pe_skips_the_bridge():
    eps = ltm_diluted_eps(
        net_income=TARGET_LTM.net_income,
        preferred_dividends=0.0,
        nci_income=0.0,
        diluted_shares=100 * M,
    ).value
    per_share = implied_per_share(
        kind=MultipleKind.PE, multiple=15.0, bridge=TARGET_BRIDGE, ltm=TARGET_LTM, diluted_eps=eps
    )
    assert per_share == pytest.approx(15.0 * 1.0)


def test_round_trip_own_multiple_recovers_own_price():
    # Applying a company's own multiple back to itself must recover its own
    # share price exactly - this holds only if the reverse bridge is the
    # exact inverse of the forward bridge (incl. short-term investments).
    eps = ltm_diluted_eps(
        net_income=TARGET_LTM.net_income,
        preferred_dividends=0.0,
        nci_income=0.0,
        diluted_shares=100 * M,
    ).value
    m = compute_multiples(
        enterprise_value=TARGET_BRIDGE.enterprise_value,
        share_price=30.0,
        ltm=TARGET_LTM,
        diluted_eps=eps,
    )
    for kind, mult in [
        (MultipleKind.EV_REVENUE, m.ev_revenue.value),
        (MultipleKind.EV_EBITDA, m.ev_ebitda.value),
        (MultipleKind.EV_EBIT, m.ev_ebit.value),
        (MultipleKind.PE, m.pe.value),
    ]:
        per_share = implied_per_share(
            kind=kind, multiple=mult, bridge=TARGET_BRIDGE, ltm=TARGET_LTM, diluted_eps=eps
        )
        assert per_share == pytest.approx(30.0), kind


def test_implied_range_low_mid_high_from_quartiles():
    stats = make_stats(p25=8.0, median=10.0, mean=10.5, p75=12.0)
    rng = implied_range(
        kind=MultipleKind.EV_EBITDA, stats=stats, bridge=TARGET_BRIDGE, ltm=TARGET_LTM
    )
    assert rng.low == pytest.approx(14.0)   # 8x -> EV 1,600 -> equity 1,400
    assert rng.mid == pytest.approx(18.0)   # median 10x
    assert rng.high == pytest.approx(22.0)  # 12x -> EV 2,400 -> equity 2,200
    assert rng.n == 5
    assert not rng.is_nm


def test_mid_basis_toggle_uses_mean():
    stats = make_stats(p25=8.0, median=10.0, mean=11.0, p75=12.0)
    rng = implied_range(
        kind=MultipleKind.EV_EBITDA,
        stats=stats,
        bridge=TARGET_BRIDGE,
        ltm=TARGET_LTM,
        mid_basis=MidBasis.MEAN,
    )
    assert rng.mid == pytest.approx(20.0)  # 11x -> EV 2,200 -> equity 2,000


def test_negative_target_metric_yields_nm_range():
    ltm = TARGET_LTM.model_copy(update={"ebitda": -50 * M})
    stats = make_stats(p25=8.0, median=10.0, mean=10.5, p75=12.0)
    rng = implied_range(kind=MultipleKind.EV_EBITDA, stats=stats, bridge=TARGET_BRIDGE, ltm=ltm)
    assert rng.is_nm
    assert rng.low is None and rng.mid is None and rng.high is None
    assert rng.nm_reason


def test_empty_peer_stats_yield_nm_range():
    empty = MultipleStats(n=0, min=None, p25=None, median=None, mean=None, p75=None, max=None)
    rng = implied_range(kind=MultipleKind.EV_EBITDA, stats=empty, bridge=TARGET_BRIDGE, ltm=TARGET_LTM)
    assert rng.is_nm
    assert rng.n == 0


def test_pe_range_with_negative_eps_is_nm():
    stats = make_stats(p25=12.0, median=15.0, mean=15.5, p75=18.0)
    rng = implied_range(
        kind=MultipleKind.PE, stats=stats, bridge=TARGET_BRIDGE, ltm=TARGET_LTM, diluted_eps=-0.4
    )
    assert rng.is_nm
