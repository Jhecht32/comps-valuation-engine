"""Service layer: composes the data layer and the valuation engine.

The API calls these and nothing else; the services call the engine and
do no arithmetic of their own.
"""

from app.services.comps import normalise_ticker, run_comps, value_company
from app.services.errors import InsufficientDataError, error_code_for, ticker_error
from app.services.models import (
    CompanyValuation,
    CompsResult,
    CompsStatistics,
    DroppedProxyPeer,
    ErrorCode,
    ImpliedValuation,
    PeerSource,
    PeerSuggestions,
    ProxyDropRule,
    ProxyFilter,
    SuggestedPeer,
    TickerError,
)
from app.services.peers import suggest_peers

__all__ = [
    "CompanyValuation",
    "CompsResult",
    "CompsStatistics",
    "DroppedProxyPeer",
    "ErrorCode",
    "ImpliedValuation",
    "InsufficientDataError",
    "PeerSource",
    "PeerSuggestions",
    "ProxyDropRule",
    "ProxyFilter",
    "SuggestedPeer",
    "TickerError",
    "error_code_for",
    "normalise_ticker",
    "run_comps",
    "suggest_peers",
    "ticker_error",
    "value_company",
]
