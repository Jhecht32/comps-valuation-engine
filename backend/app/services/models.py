"""Result models for the service layer, also the API's response models.

Everything here is assembled from engine and provider models; the only
new information is the per-ticker error shape and the peer-suggestion
wrapper. Monetary amounts stay in raw currency units.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel

from app.data.peers import MarketCapBand, MatchBasis
from app.data.provider import CompanyProfile
from app.valuation.models import (
    DataQualityFlag,
    EVBridge,
    ImpliedRange,
    LTMFinancials,
    MidBasis,
    Multiples,
    MultipleStats,
)


# --- Errors ---------------------------------------------------------------


class ErrorCode(str, Enum):
    TICKER_NOT_FOUND = "ticker_not_found"
    CURRENCY_MISMATCH = "currency_mismatch"
    INSUFFICIENT_DATA = "insufficient_data"
    SOURCE_ERROR = "source_error"
    PEER_DISCOVERY_UNAVAILABLE = "peer_discovery_unavailable"
    INVALID_REQUEST = "invalid_request"


class TickerError(BaseModel):
    """Why one ticker could not be valued. Returned per peer inside a
    comps result and as the body of every error response, including
    request validation failures (ticker None, code invalid_request), so
    a client needs one error shape."""

    ticker: str | None
    code: ErrorCode
    message: str


# --- One company ----------------------------------------------------------


class CompanyValuation(BaseModel):
    """A snapshot run through the engine: bridge, LTM, multiples, and the
    union of every data-quality flag raised along the way."""

    ticker: str
    name: str | None
    source: str
    price_currency: str | None
    reporting_currency: str | None
    share_price: float
    price_as_of: date
    shares_as_of: date | None
    balance_sheet_as_of: date
    bridge: EVBridge
    ltm: LTMFinancials
    diluted_eps: float | None
    multiples: Multiples
    flags: list[DataQualityFlag]


# --- Comps run ------------------------------------------------------------


class CompsStatistics(BaseModel):
    ev_revenue: MultipleStats
    ev_ebitda: MultipleStats
    ev_ebit: MultipleStats
    pe: MultipleStats


class ImpliedValuation(BaseModel):
    mid_basis: MidBasis
    ev_revenue: ImpliedRange
    ev_ebitda: ImpliedRange
    ev_ebit: ImpliedRange
    pe: ImpliedRange


class CompsResult(BaseModel):
    """Full output of one comps run.

    Tickers are normalised (stripped, upper-cased) and de-duplicated,
    and the target is dropped from the peer list. peers holds every peer
    that was valued, in that normalised request order; errors holds every
    peer that was not, keyed by normalised ticker, so one bad name never
    hides the rest.
    """

    as_of: date
    target: CompanyValuation
    peers: list[CompanyValuation]
    statistics: CompsStatistics
    implied: ImpliedValuation
    errors: dict[str, TickerError]


# --- Peer suggestions -----------------------------------------------------


class SuggestedPeer(CompanyProfile):
    match_basis: MatchBasis


class PeerSuggestions(BaseModel):
    """Candidates from the industry screen, closest in size first, then
    (when the industry was thin) sector names closest in size, each
    labelled with its match_basis. flags carries PEER_SET_WIDENED when
    sector names were sought and THIN_PEER_SET when the set is still
    short of MIN_PEERS."""

    target: CompanyProfile
    market_cap_band: MarketCapBand
    peers: list[SuggestedPeer]
    flags: list[DataQualityFlag]
