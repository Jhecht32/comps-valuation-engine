"""Pure valuation logic for the comps engine.

Everything in this package is deterministic and network-free: it takes
already-fetched financial data and produces the comps table, statistics,
and implied valuation. The API layer composes these; the data layer
(MarketDataProvider) supplies the inputs.
"""

from app.valuation.ev_bridge import build_ev_bridge
from app.valuation.implied import implied_per_share, implied_range
from app.valuation.ltm import ltm_from_annual_and_stub, ltm_from_quarters
from app.valuation.models import (
    AnnualFinancials,
    BalanceSheetItems,
    CompanySnapshot,
    DataQualityFlag,
    EVBridge,
    FlagCode,
    ImpliedRange,
    LTMFinancials,
    LTMMethod,
    MidBasis,
    MultipleKind,
    Multiples,
    MultipleStats,
    MultipleValue,
    PeriodFinancials,
    QuarterlyFinancials,
)
from app.valuation.multiples import EV_EBITDA_NM_CAP, compute_multiples, ebitda_margin, ltm_diluted_eps
from app.valuation.stats import percentile_inc, summarize_multiples

__all__ = [
    "AnnualFinancials",
    "BalanceSheetItems",
    "CompanySnapshot",
    "DataQualityFlag",
    "EVBridge",
    "EV_EBITDA_NM_CAP",
    "FlagCode",
    "ImpliedRange",
    "LTMFinancials",
    "LTMMethod",
    "MidBasis",
    "MultipleKind",
    "Multiples",
    "MultipleStats",
    "MultipleValue",
    "PeriodFinancials",
    "QuarterlyFinancials",
    "build_ev_bridge",
    "compute_multiples",
    "ebitda_margin",
    "implied_per_share",
    "implied_range",
    "ltm_diluted_eps",
    "ltm_from_annual_and_stub",
    "ltm_from_quarters",
    "percentile_inc",
    "summarize_multiples",
]
