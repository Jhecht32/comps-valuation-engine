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
    # Raised by peer suggestion (app.services.peers), not by the engine.
    SCREEN_ON_SECTOR = "screen_on_sector"  # the target has no industry classification; its sector was screened, loosely
    THIN_PEER_SET = "thin_peer_set"        # both sources together gave fewer names than a usable set needs
    PROXY_PEERS_UNAVAILABLE = "proxy_peers_unavailable"        # no proxy statement, or its peer group could not be read or gave nothing
    PROXY_PEERS_LOW_CONFIDENCE = "proxy_peers_low_confidence"  # a list was found but too few names mapped to trust it
    PROXY_PEERS_UNMATCHED = "proxy_peers_unmatched"            # names in the proxy peer group with no ticker; add by hand
    PROXY_PEERS_DROPPED = "proxy_peers_dropped"                # proxy names filtered out: other sector, margin too far, or unvaluable
    PROXY_FILTER_PARTIAL = "proxy_filter_partial"              # the sector or margin test could not be applied to the proxy group


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

    net_income is consolidated net income before the two income-to-common
    deductions below. preferred_dividends and nci_income (income
    attributable to noncontrolling interests, an income statement line,
    not the balance sheet NCI) are None when the source does not break
    them out and 0.0 when they are known to be zero; the distinction
    drives the EPS_FROM_CONSOLIDATED_NI flag downstream.
    """

    revenue: float | None = None
    gross_profit: float | None = None
    ebit: float | None = None
    net_income: float | None = None
    d_and_a: float | None = None
    preferred_dividends: float | None = None
    nci_income: float | None = None


class QuarterlyFinancials(PeriodFinancials):
    period_end: date


class AnnualFinancials(PeriodFinancials):
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
    # Income-to-common breakouts stitched over the same window; None means
    # not broken out in at least one period (no flag here, the EPS
    # calculation flags the consolidated fallback).
    preferred_dividends: float | None = None
    nci_income: float | None = None


# --- Company snapshot (provider output, engine input) --------------------


class BalanceSheetItems(BaseModel):
    """EV bridge inputs from the most recent balance sheet.

    None means the source has no such line at all; 0.0 means the line is
    present and zero. build_ev_bridge turns that into the *_reported
    booleans, so a provider must never collapse "absent" into 0.0.
    """

    as_of: date
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    short_term_investments: float | None = None
    preferred_equity: float | None = None
    noncontrolling_interest: float | None = None


class CompanySnapshot(BaseModel):
    """Everything the engine needs about one company, as the source
    reported it, in raw dollars.

    This is the contract between MarketDataProvider implementations and
    the valuation module: a provider maps raw source data into this shape
    and does nothing else (no LTM stitching, no fallbacks, no derived
    figures). Periods are ordered newest first; the engine selects the
    window it needs. Line items follow the PeriodFinancials conventions:
    None for "not reported / not broken out", 0.0 for known-zero.
    """

    ticker: str
    name: str | None = None

    # Listing currency of the share price vs. currency of the financial
    # statements. They diverge for ADRs and dual listings; the provider
    # layer refuses to hand out a snapshot where they differ.
    price_currency: str | None = None
    reporting_currency: str | None = None

    share_price: float
    price_as_of: date

    # Diluted count is the primary EV input; basic is the flagged fallback.
    diluted_shares: float | None = None
    basic_shares: float | None = None
    shares_as_of: date | None = None

    balance_sheet: BalanceSheetItems
    quarters: list[QuarterlyFinancials]
    fiscal_years: list[AnnualFinancials]

    source: str


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
