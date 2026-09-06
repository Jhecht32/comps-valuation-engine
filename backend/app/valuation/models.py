"""Shared data models for the valuation engine.

Conventions:
- All monetary amounts are raw dollars (not millions). Formatting for
  display is the UI's job.
- None means "not reported", which is different from zero. Every fallback
  or gap becomes a DataQualityFlag that travels with the object it
  describes, so the API layer can surface every approximation without
  re-deriving it.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel


# --- Data quality -------------------------------------------------------


class FlagCode(str, Enum):
    BASIC_SHARES_FALLBACK = "basic_shares_fallback"
    MISSING_TOTAL_DEBT = "missing_total_debt"
    MISSING_CASH = "missing_cash"
    MISSING_DA = "missing_da"
    MISSING_LINE_ITEM = "missing_line_item"
    LTM_FROM_ANNUAL_STUB = "ltm_from_annual_stub"
    NONCONSECUTIVE_QUARTERS = "nonconsecutive_quarters"
    STALE_FILING = "stale_filing"
    EPS_FROM_CONSOLIDATED_NI = "eps_from_consolidated_ni"


class DataQualityFlag(BaseModel):
    code: FlagCode
    message: str


# --- Reported periods and LTM -------------------------------------------


class LTMMethod(str, Enum):
    QUARTERLY = "quarterly"          # sum of the four most recent quarters
    ANNUAL_STUB = "annual_stub"      # FY + current YTD stub - prior-year stub


class PeriodFinancials(BaseModel):
    """Income statement metrics for one reported period.

    d_and_a comes from the cash flow statement, not the income statement.
    """

    revenue: float | None = None
    gross_profit: float | None = None
    ebit: float | None = None
    net_income: float | None = None
    d_and_a: float | None = None


class QuarterlyFinancials(PeriodFinancials):
    period_end: date


class LTMFinancials(BaseModel):
    revenue: float | None
    gross_profit: float | None
    ebitda: float | None
    ebit: float | None
    net_income: float | None
    as_of: date
    method: LTMMethod
    flags: list[DataQualityFlag]


# --- Enterprise value bridge --------------------------------------------


class EVBridge(BaseModel):
    """Resolved EV bridge for one company.

    The *_reported booleans preserve whether preferred, NCI, and
    short-term investments were present in the source (possibly as zero)
    or absent entirely; the float fields always hold the effective value
    used in the bridge, so arithmetic consumers never see None.
    """

    share_price: float
    diluted_shares: float
    shares_are_basic_fallback: bool
    equity_value: float
    total_debt: float
    preferred_equity: float
    preferred_equity_reported: bool
    noncontrolling_interest: float
    noncontrolling_interest_reported: bool
    cash_and_equivalents: float
    short_term_investments: float
    short_term_investments_reported: bool
    enterprise_value: float
    flags: list[DataQualityFlag]


# --- Multiples ----------------------------------------------------------


class MultipleKind(str, Enum):
    EV_REVENUE = "ev_revenue"
    EV_EBITDA = "ev_ebitda"
    EV_EBIT = "ev_ebit"
    PE = "pe"


class MultipleValue(BaseModel):
    """One computed multiple, or an explicit NM with the reason why.

    value is None exactly when is_nm is True; NM multiples are excluded
    from statistics and rendered as "NM", never as a number.
    """

    value: float | None
    is_nm: bool
    nm_reason: str | None = None


class DilutedEPS(BaseModel):
    """LTM diluted EPS on an income-to-common basis.

    value = (LTM net income - preferred dividends - income attributable
    to NCI) / current diluted shares. When a breakout is unavailable the
    consolidated fallback is flagged, never silent.
    """

    value: float | None
    flags: list[DataQualityFlag]


class Multiples(BaseModel):
    ev_revenue: MultipleValue
    ev_ebitda: MultipleValue
    ev_ebit: MultipleValue
    pe: MultipleValue


class MultipleStats(BaseModel):
    """Distribution of one multiple across the included peer set.

    n is the number of companies actually informing the statistics after
    NM exclusion — "a median of 8" vs "a median of 3" matters.
    """

    n: int
    min: float | None
    p25: float | None
    median: float | None
    mean: float | None
    p75: float | None
    max: float | None


# --- Implied valuation --------------------------------------------------


class MidBasis(str, Enum):
    MEDIAN = "median"
    MEAN = "mean"


class ImpliedRange(BaseModel):
    """Low/mid/high implied per-share values for one methodology.

    Low and high come from the peer 25th/75th percentiles; mid comes from
    the median (default) or mean per the mid_basis toggle.
    """

    kind: MultipleKind
    low: float | None
    mid: float | None
    high: float | None
    n: int
    is_nm: bool
    nm_reason: str | None = None
