"""Tests for LTM (last twelve months) stitching.

Primary path: sum of the four most recent reported quarters.
Fallback path: FY + current YTD stub - prior-year comparable stub.
"""

from datetime import date, timedelta

import pytest

from app.valuation.ltm import ltm_from_annual_and_stub, ltm_from_quarters
from app.valuation.models import FlagCode, LTMMethod, PeriodFinancials, QuarterlyFinancials

M = 1_000_000


def quarter(end, revenue, gross_profit, ebit, net_income, d_and_a):
    return QuarterlyFinancials(
        period_end=end,
        revenue=revenue,
        gross_profit=gross_profit,
        ebit=ebit,
        net_income=net_income,
        d_and_a=d_and_a,
    )


CLEAN_QUARTERS = [
    quarter(date(2025, 6, 30), 260 * M, 130 * M, 52 * M, 39 * M, 26 * M),
    quarter(date(2025, 3, 31), 250 * M, 125 * M, 50 * M, 37 * M, 25 * M),
    quarter(date(2024, 12, 31), 240 * M, 120 * M, 48 * M, 36 * M, 24 * M),
    quarter(date(2024, 9, 30), 230 * M, 115 * M, 46 * M, 34 * M, 23 * M),
]


def test_four_clean_quarters_sum_to_ltm():
    ltm = ltm_from_quarters(CLEAN_QUARTERS)
    assert ltm.revenue == pytest.approx(980 * M)
    assert ltm.gross_profit == pytest.approx(490 * M)
    assert ltm.ebit == pytest.approx(196 * M)
    assert ltm.net_income == pytest.approx(146 * M)
    # EBITDA = EBIT + D&A (D&A sourced from the cash flow statement)
    assert ltm.ebitda == pytest.approx(196 * M + 98 * M)
    assert ltm.as_of == date(2025, 6, 30)
    assert ltm.method == LTMMethod.QUARTERLY
    assert ltm.flags == []


def test_uses_four_most_recent_when_more_are_given():
    extra = [quarter(date(2024, 6, 30), 999 * M, 500 * M, 99 * M, 77 * M, 55 * M)]
    ltm = ltm_from_quarters(CLEAN_QUARTERS + extra)
    assert ltm.revenue == pytest.approx(980 * M)  # 5th-oldest quarter ignored


def test_unsorted_input_is_handled():
    ltm = ltm_from_quarters(list(reversed(CLEAN_QUARTERS)))
    assert ltm.revenue == pytest.approx(980 * M)
    assert ltm.as_of == date(2025, 6, 30)


def test_fewer_than_four_quarters_raises():
    with pytest.raises(ValueError):
        ltm_from_quarters(CLEAN_QUARTERS[:3])


def test_missing_d_and_a_suppresses_ebitda_and_flags():
    quarters = [q.model_copy() for q in CLEAN_QUARTERS]
    quarters[2] = quarters[2].model_copy(update={"d_and_a": None})
    ltm = ltm_from_quarters(quarters)
    assert ltm.ebitda is None
    assert ltm.ebit == pytest.approx(196 * M)  # EBIT itself is unaffected
    assert FlagCode.MISSING_DA in [f.code for f in ltm.flags]


def test_missing_line_item_in_one_quarter_suppresses_that_metric():
    quarters = [q.model_copy() for q in CLEAN_QUARTERS]
    quarters[0] = quarters[0].model_copy(update={"ebit": None})
    ltm = ltm_from_quarters(quarters)
    assert ltm.ebit is None
    assert ltm.revenue == pytest.approx(980 * M)
    assert FlagCode.MISSING_LINE_ITEM in [f.code for f in ltm.flags]


def test_missing_gross_profit_is_none_without_flag():
    # No multiple uses gross profit; banks and insurers never report it.
    quarters = [q.model_copy() for q in CLEAN_QUARTERS]
    quarters[0] = quarters[0].model_copy(update={"gross_profit": None})
    ltm = ltm_from_quarters(quarters)
    assert ltm.gross_profit is None
    assert ltm.flags == []


def test_annual_plus_stub_missing_gross_profit_is_none_without_flag():
    fy = FY.model_copy(update={"gross_profit": None})
    ltm = ltm_from_annual_and_stub(fy=fy, ytd_current=YTD_CURRENT, ytd_prior=YTD_PRIOR)
    assert ltm.gross_profit is None
    assert [f.code for f in ltm.flags] == [FlagCode.LTM_FROM_ANNUAL_STUB]


def test_gap_in_quarters_is_flagged():
    # 2024-12-31 quarter missing: the four most recent span ~5 quarters.
    quarters = [
        quarter(date(2025, 6, 30), 260 * M, 130 * M, 52 * M, 39 * M, 26 * M),
        quarter(date(2025, 3, 31), 250 * M, 125 * M, 50 * M, 37 * M, 25 * M),
        quarter(date(2024, 9, 30), 230 * M, 115 * M, 46 * M, 34 * M, 23 * M),
        quarter(date(2024, 6, 30), 220 * M, 110 * M, 44 * M, 33 * M, 22 * M),
    ]
    ltm = ltm_from_quarters(quarters)
    assert FlagCode.NONCONSECUTIVE_QUARTERS in [f.code for f in ltm.flags]


def test_gap_check_measures_span_between_period_end_dates():
    """Pins the gap-check semantics: the 320-day threshold applies to
    (newest period_end - oldest period_end) across the four selected
    quarters. Four consecutive quarter-ends span ~273 days on this
    measure; one missing quarter stretches it to ~365. It does NOT
    measure fiscal coverage from the earliest quarter's start to the
    latest quarter's end (which is ~365 days even with no gap).
    """

    def quarters_spanning(days):
        oldest = date(2024, 9, 30)
        ends = [
            oldest,
            oldest + timedelta(days=91),
            oldest + timedelta(days=182),
            oldest + timedelta(days=days),  # newest
        ]
        return [quarter(e, 250 * M, 125 * M, 50 * M, 37 * M, 25 * M) for e in ends]

    at_threshold = ltm_from_quarters(quarters_spanning(320))
    assert FlagCode.NONCONSECUTIVE_QUARTERS not in [f.code for f in at_threshold.flags]

    past_threshold = ltm_from_quarters(quarters_spanning(321))
    assert FlagCode.NONCONSECUTIVE_QUARTERS in [f.code for f in past_threshold.flags]


def test_stale_filing_is_flagged():
    ltm = ltm_from_quarters(CLEAN_QUARTERS, today=date(2026, 3, 1))
    assert FlagCode.STALE_FILING in [f.code for f in ltm.flags]


def test_recent_filing_is_not_flagged_stale():
    ltm = ltm_from_quarters(CLEAN_QUARTERS, today=date(2025, 8, 15))
    assert FlagCode.STALE_FILING not in [f.code for f in ltm.flags]


# --- Annual + stub fallback ---------------------------------------------

FY = PeriodFinancials(
    revenue=1_000 * M, gross_profit=500 * M, ebit=200 * M, net_income=150 * M, d_and_a=100 * M
)
YTD_CURRENT = QuarterlyFinancials(
    period_end=date(2025, 6, 30),
    revenue=550 * M,
    gross_profit=280 * M,
    ebit=110 * M,
    net_income=82 * M,
    d_and_a=52 * M,
)
YTD_PRIOR = PeriodFinancials(
    revenue=480 * M, gross_profit=240 * M, ebit=95 * M, net_income=70 * M, d_and_a=48 * M
)


def test_annual_plus_stub_arithmetic():
    ltm = ltm_from_annual_and_stub(fy=FY, ytd_current=YTD_CURRENT, ytd_prior=YTD_PRIOR)
    # LTM = FY + current stub - prior-year comparable stub
    assert ltm.revenue == pytest.approx((1_000 + 550 - 480) * M)
    assert ltm.ebit == pytest.approx((200 + 110 - 95) * M)
    assert ltm.net_income == pytest.approx((150 + 82 - 70) * M)
    assert ltm.ebitda == pytest.approx((200 + 110 - 95) * M + (100 + 52 - 48) * M)
    assert ltm.as_of == date(2025, 6, 30)
    assert ltm.method == LTMMethod.ANNUAL_STUB


def test_annual_plus_stub_is_always_flagged_as_approximation():
    ltm = ltm_from_annual_and_stub(fy=FY, ytd_current=YTD_CURRENT, ytd_prior=YTD_PRIOR)
    assert FlagCode.LTM_FROM_ANNUAL_STUB in [f.code for f in ltm.flags]


def test_annual_plus_stub_missing_piece_suppresses_metric():
    ytd_prior = YTD_PRIOR.model_copy(update={"ebit": None})
    ltm = ltm_from_annual_and_stub(fy=FY, ytd_current=YTD_CURRENT, ytd_prior=ytd_prior)
    assert ltm.ebit is None
    assert ltm.revenue == pytest.approx((1_000 + 550 - 480) * M)
    assert FlagCode.MISSING_LINE_ITEM in [f.code for f in ltm.flags]


# --- Income-to-common breakouts -------------------------------------------
#
# preferred_dividends and nci_income are stitched like any other income
# statement line, but None means "not broken out by the source" rather
# than "missing valuation input": it propagates to the LTM result without
# a MISSING_LINE_ITEM flag, and ltm_diluted_eps decides how to flag it.


def test_breakouts_are_summed_across_quarters():
    quarters = [
        q.model_copy(update={"preferred_dividends": 1 * M, "nci_income": 2 * M})
        for q in CLEAN_QUARTERS
    ]
    ltm = ltm_from_quarters(quarters)
    assert ltm.preferred_dividends == pytest.approx(4 * M)
    assert ltm.nci_income == pytest.approx(8 * M)
    assert ltm.flags == []


def test_known_zero_breakouts_survive_as_zero():
    quarters = [
        q.model_copy(update={"preferred_dividends": 0.0, "nci_income": 0.0})
        for q in CLEAN_QUARTERS
    ]
    ltm = ltm_from_quarters(quarters)
    assert ltm.preferred_dividends == 0.0
    assert ltm.nci_income == 0.0


def test_unbroken_out_breakout_in_one_quarter_is_none_without_flag():
    quarters = [
        q.model_copy(update={"preferred_dividends": 0.0, "nci_income": 2 * M})
        for q in CLEAN_QUARTERS
    ]
    quarters[1] = quarters[1].model_copy(update={"nci_income": None})
    ltm = ltm_from_quarters(quarters)
    assert ltm.nci_income is None
    assert ltm.preferred_dividends == 0.0
    assert FlagCode.MISSING_LINE_ITEM not in [f.code for f in ltm.flags]


def test_breakouts_default_to_none_when_never_supplied():
    ltm = ltm_from_quarters(CLEAN_QUARTERS)
    assert ltm.preferred_dividends is None
    assert ltm.nci_income is None
    assert FlagCode.MISSING_LINE_ITEM not in [f.code for f in ltm.flags]


def test_annual_plus_stub_stitches_breakouts():
    fy = FY.model_copy(update={"preferred_dividends": 10 * M, "nci_income": 20 * M})
    cur = YTD_CURRENT.model_copy(update={"preferred_dividends": 6 * M, "nci_income": 12 * M})
    prior = YTD_PRIOR.model_copy(update={"preferred_dividends": 5 * M, "nci_income": 9 * M})
    ltm = ltm_from_annual_and_stub(fy=fy, ytd_current=cur, ytd_prior=prior)
    assert ltm.preferred_dividends == pytest.approx((10 + 6 - 5) * M)
    assert ltm.nci_income == pytest.approx((20 + 12 - 9) * M)
    assert FlagCode.MISSING_LINE_ITEM not in [f.code for f in ltm.flags]


def test_annual_plus_stub_unbroken_out_breakout_is_none_without_flag():
    fy = FY.model_copy(update={"nci_income": None})
    cur = YTD_CURRENT.model_copy(update={"nci_income": 12 * M})
    prior = YTD_PRIOR.model_copy(update={"nci_income": 9 * M})
    ltm = ltm_from_annual_and_stub(fy=fy, ytd_current=cur, ytd_prior=prior)
    assert ltm.nci_income is None
    assert FlagCode.MISSING_LINE_ITEM not in [f.code for f in ltm.flags]
