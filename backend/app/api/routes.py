"""The three endpoints. Handlers are plain functions, so Starlette runs
them in its worker-thread pool and the synchronous provider never
blocks the event loop.

Failure classes map to status codes in app.main:
    unknown ticker            -> 404
    currency mismatch         -> 422
    insufficient data         -> 422
    peer discovery unsupported-> 501
    source failure            -> 502
A peer failing inside POST /api/comps is not an HTTP error: it is
reported in the response's errors map next to the peers that succeeded.
"""

from fastapi import APIRouter

from app.api.deps import ProviderDep, TodayDep
from app.api.schemas import (
    CompanyResponse,
    CompsRequest,
    CompsResponse,
    ErrorResponse,
    HealthResponse,
    PeersResponse,
    TickerPath,
)
from app.services import run_comps, suggest_peers, value_company

router = APIRouter(prefix="/api")

_ERRORS = {
    404: {"model": ErrorResponse, "description": "Ticker not found"},
    422: {"model": ErrorResponse, "description": "Cannot be valued (currency mismatch or insufficient data)"},
    502: {"model": ErrorResponse, "description": "Market data source failed"},
}


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/company/{ticker}", response_model=CompanyResponse, responses=_ERRORS)
def get_company(ticker: TickerPath, provider: ProviderDep, today: TodayDep) -> CompanyResponse:
    """One company's snapshot with its EV bridge, LTM figures, multiples,
    and data-quality flags."""
    snapshot = provider.get_company(ticker)
    return value_company(snapshot, today=today)


@router.post("/comps", response_model=CompsResponse, responses=_ERRORS)
def post_comps(body: CompsRequest, provider: ProviderDep, today: TodayDep) -> CompsResponse:
    """Full comps run: target, valued peers, statistics with n-counts,
    implied valuation range, and per-ticker errors for peers that could
    not be valued."""
    return run_comps(
        provider,
        target=body.target,
        peers=body.peers,
        mid_basis=body.mid_basis,
        today=today,
    )


@router.get(
    "/peers/{ticker}",
    response_model=PeersResponse,
    responses={**_ERRORS, 501: {"model": ErrorResponse, "description": "Source cannot screen for peers"}},
)
def get_peers(ticker: TickerPath, provider: ProviderDep) -> PeersResponse:
    """Suggested peers: same industry, market cap within 0.33x-3.0x of the
    target, same currencies. Never widened to the sector; a set of fewer
    than three names is returned as found with a thin_peer_set flag."""
    return suggest_peers(provider, ticker)
