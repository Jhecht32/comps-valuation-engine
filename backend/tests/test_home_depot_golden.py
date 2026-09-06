"""Golden-model validation test: Home Depot, LTM through 2026-08-02.

This is a validation test against a hand-built banker's model, not a unit
test. It runs one real company end to end — annual+stub LTM, EV bridge,
EPS, multiples — and asserts the exact figures the hand model produces.
If it breaks, either the engine's methodology changed or the golden model
needs re-deriving; neither should happen silently.

Source filings: HD 10-Q for the quarter ended 2026-08-02 (YTD = six
months) and 10-K for FY2025 (52 weeks ended 2026-02-01). All figures $mm
except shares (millions) and share price ($); the engine is unit-agnostic
and shares-in-millions times price-in-dollars yields equity value in $mm.

Methodology notes recorded with the model:

- D&A is the sum of both cash flow statement lines: depreciation
  (excluding intangible asset amortization) plus intangible asset
  amortization. HD's income statement D&A line (1,693 for the half,
  3,273 for FY2025) excludes intangible amortization and would
  understate EBITDA by roughly $850mm annually.
- Operating lease liabilities of 9,671 are excluded from the EV bridge
  to stay consistent with unadjusted (pre-IFRS-16-style) EBITDA.
- The LTM window spans the SRS/GMS acquisition; no pro forma adjustment
  is made, so LTM mixes pre- and post-acquisition periods.
- FY2025 was 52 weeks vs. 53 weeks in FY2024, so the annual+stub LTM
  will not tie exactly to a terminal's LTM figure.

The balance sheet inputs deliberately exercise the absent-vs-known-zero
distinction end to end: short-term investments are absent from HD's
balance sheet (None -> zero, reported=False), while preferred equity and
noncontrolling interest are known-zero (0.0 -> reported=True).
"""

from datetime import date

from app.valuation.ev_bridge import build_ev_bridge
from app.valuation.ltm import ltm_from_annual_and_stub
from app.valuation.models import FlagCode, LTMMethod, PeriodFinancials, QuarterlyFinancials
from app.valuation.multiples import compute_multiples, ltm_diluted_eps

# --- Inputs, straight off the filings ($mm) ------------------------------

FY2025 = PeriodFinancials(  # 10-K, 52 weeks ended 2026-02-01
    revenue=164_683,
    ebit=20_890,
    net_income=14_156,
    d_and_a=4_121,
)
YTD_CURRENT = QuarterlyFinancials(  # 10-Q, six months ended 2026-08-02
    period_end=date(2026, 8, 2),
    revenue=89_626,
    ebit=11_820,
    net_income=8_055,
    d_and_a=2_188,
)
YTD_PRIOR = PeriodFinancials(  # 10-Q comparative, six months ended 2025-08-03
    revenue=85_133,
    ebit=11_688,
    net_income=7_984,
    d_and_a=1_998,
)

# Balance sheet at 2026-08-02
CASH = 2_085
SHORT_TERM_DEBT = 4_248
CURRENT_LTD = 4_697
LONG_TERM_DEBT = 43_951

DILUTED_SHARES = 996  # millions
SHARE_PRICE = 321.05  # $, close of 2026-09-04
PRICE_DATE = date(2026, 9, 4)


def build_golden_model():
    ltm = ltm_from_annual_and_stub(
        fy=FY2025,
        ytd_current=YTD_CURRENT,
        ytd_prior=YTD_PRIOR,
        today=PRICE_DATE,
    )
    bridge = build_ev_bridge(
        share_price=SHARE_PRICE,
        diluted_shares=DILUTED_SHARES,
        total_debt=SHORT_TERM_DEBT + CURRENT_LTD + LONG_TERM_DEBT,
        preferred_equity=0.0,  # known-zero: no preferred on the balance sheet
        noncontrolling_interest=0.0,  # known-zero
        cash_and_equivalents=CASH,
        short_term_investments=None,  # absent from the balance sheet entirely
    )
    eps = ltm_diluted_eps(
        net_income=ltm.net_income,
        preferred_dividends=0.0,
        nci_income=0.0,
        diluted_shares=DILUTED_SHARES,
    )
    multiples = compute_multiples(
        enterprise_value=bridge.enterprise_value,
        share_price=SHARE_PRICE,
        ltm=ltm,
        diluted_eps=eps.value,
    )
    return ltm, bridge, eps, multiples


def test_ltm_ties_to_hand_model():
    ltm, _, _, _ = build_golden_model()
    assert ltm.method == LTMMethod.ANNUAL_STUB
    assert ltm.as_of == date(2026, 8, 2)
    assert ltm.revenue == 169_176
    assert ltm.ebit == 21_022
    assert ltm.net_income == 14_227
    assert ltm.ebitda == 25_333  # EBIT 21,022 + D&A 4,311
    # Expected flags only: the annual-stub method note. Gross profit is not
    # part of the golden model but is informational, so its absence is not
    # flagged. No staleness as of the price date.
    assert [f.code for f in ltm.flags] == [FlagCode.LTM_FROM_ANNUAL_STUB]


def test_ev_bridge_ties_to_hand_model():
    _, bridge, _, _ = build_golden_model()
    assert bridge.equity_value == 319_765.8  # 996 x 321.05, exact
    assert bridge.enterprise_value == 370_576.8  # + debt 52,896 - cash 2,085, exact
    assert bridge.flags == []
    # Absent-vs-known-zero: same effective zero, different provenance.
    assert bridge.short_term_investments == 0.0
    assert bridge.short_term_investments_reported is False
    assert bridge.preferred_equity == 0.0
    assert bridge.preferred_equity_reported is True
    assert bridge.noncontrolling_interest == 0.0
    assert bridge.noncontrolling_interest_reported is True


def test_multiples_tie_to_hand_model():
    _, _, eps, multiples = build_golden_model()
    assert eps.flags == []  # known-zero breakouts, no consolidated fallback
    assert round(eps.value, 3) == 14.284
    for m in (multiples.ev_revenue, multiples.ev_ebitda, multiples.ev_ebit, multiples.pe):
        assert not m.is_nm
    assert round(multiples.ev_revenue.value, 3) == 2.190
    assert round(multiples.ev_ebitda.value, 3) == 14.628
    assert round(multiples.ev_ebit.value, 3) == 17.628
    assert round(multiples.pe.value, 3) == 22.476
