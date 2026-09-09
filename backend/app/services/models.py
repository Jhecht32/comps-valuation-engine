"""Result models for the service layer, also the API's response models.

Everything here is assembled from engine and provider models; the only
new information is the per-ticker error shape and the peer-suggestion
wrapper. Monetary amounts stay in raw currency units.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel

from app.data.peers import MarketCapBand
from app.data.provider import CompanyProfile
from app.data.proxy_peers import ProxyPeerGroup
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


class PeerSource(str, Enum):
    PROXY = "proxy"    # the compensation peer group in the target's proxy statement, filtered for comparability
    SCREEN = "screen"  # companies Yahoo files in the target's industry, inside the market-cap band


class SuggestedPeer(CompanyProfile):
    """A candidate peer with the source(s) that named it. Both sources
    naming one company is the strongest signal the tool can give."""

    sources: list[PeerSource]
    ebitda_margin: float | None = None  # LTM, a fraction; set for proxy names, which are filtered on it


class ProxyDropRule(str, Enum):
    SECTOR = "sector"          # outside the target's sector
    MARGIN = "margin"          # LTM EBITDA margin further from the target's than the tolerance
    NO_MARGIN = "no_margin"    # LTM EBITDA margin could not be computed
    UNVALUABLE = "unvaluable"  # no market data, too little of it, or another currency


class DroppedProxyPeer(BaseModel):
    """A name from the proxy peer group left out of the suggestion, with
    enough detail to judge whether to add it back by hand."""

    ticker: str
    name: str
    rule: ProxyDropRule
    reason: str
    sector: str | None = None
    industry: str | None = None
    ebitda_margin: float | None = None


class ProxyFilter(BaseModel):
    """The business-comparability test applied to a proxy peer group: a
    peer must share the target's sector, and its LTM EBITDA margin must
    sit within margin_band of the target's in relative terms (0.5 = no
    more than 50% above or below, so 5.0%-14.9% for a 9.9% target);
    margin_low and margin_high are that band's edges. A None sector or
    margin means that test could not be applied and was skipped, which
    PROXY_FILTER_PARTIAL flags."""

    sector: str | None
    ebitda_margin: float | None
    margin_band: float
    margin_low: float | None = None
    margin_high: float | None = None


class PeerSuggestions(BaseModel):
    """Candidate peers from two sources, combined rather than ranked.

    peers is the union of the filtered proxy peer group and the industry
    screen, each name tagged with the source(s) that suggested it and
    ordered with the strongest signal first: names both sources agree
    on, then the rest of the proxy group in the filer's order, then the
    rest of the screen closest in size. Neither source is a peer set on
    its own (a compensation peer group reflects pay benchmarking, an
    industry screen is coarse), so the list is a starting point for the
    reader to curate.

    proxy_label and screen_label are the sentences a UI shows for each
    source; proxy_label is None when no proxy peer group could be read
    (no filing, unreadable, low confidence), and the flags say why. It
    is set even when the filter removed every name, so the disclosure
    and the drop list stay on show.
    proxy carries the proxy-statement extraction whenever one was read,
    even at low confidence, so its raw list, unmapped names, and
    confidence stay visible; proxy_filter and proxy_dropped say what
    comparability test was applied and which names it removed, each
    with its reason, so a reader can add any of them back. flags
    explains every omission: PROXY_* for the proxy step, SCREEN_ON_SECTOR
    when the target has no industry to screen on, THIN_PEER_SET when the
    union is short of MIN_PEERS."""

    target: CompanyProfile
    market_cap_band: MarketCapBand
    proxy_label: str | None = None
    screen_label: str
    proxy: ProxyPeerGroup | None = None
    proxy_filter: ProxyFilter | None = None
    proxy_dropped: list[DroppedProxyPeer] = []
    peers: list[SuggestedPeer]
    flags: list[DataQualityFlag]
