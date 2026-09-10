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

from fastapi import APIRouter, Response

from app.api.deps import ProviderDep, ProxySourceDep, TodayDep
from app.api.schemas import (
    CompanyResponse,
    CompsRequest,
    CompsResponse,
    ErrorResponse,
    HealthResponse,
    PeersResponse,
    TickerPath,
)
from app.data.provider import MarketDataError
from app.services import (
    XLSX_MEDIA_TYPE,
    InsufficientDataError,
    build_workbook,
    export_filename,
    run_comps,
    suggest_peers,
    value_company,
)

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


@router.post(
    "/comps/export",
    response_class=Response,
    responses={
        **_ERRORS,
        200: {
            "content": {XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
            "description": "Workbook with Comps, Statistics, Implied, and Sources sheets",
        },
    },
)
def post_comps_export(body: CompsRequest, provider: ProviderDep, proxy_source: ProxySourceDep, today: TodayDep) -> Response:
    """The same run as POST /api/comps, as a formatted .xlsx attachment
    named {TICKER}_comps_{YYYY-MM-DD}.xlsx. The Sources sheet adds the
    audit trail: the as-of dates, the proxy filing, every candidate
    with its source tags, every name filtered out with its reason, and
    every flag. The peer suggestion is re-run for it; if that fails the
    sheet says so and the export still succeeds."""
    result = run_comps(provider, target=body.target, peers=body.peers, mid_basis=body.mid_basis, today=today)
    suggestions, why = None, None
    try:
        suggestions = suggest_peers(provider, body.target, proxy_source=proxy_source, today=today)
    except (MarketDataError, InsufficientDataError) as err:
        why = str(err)
    content = build_workbook(result, suggestions, requested_peers=body.peers, suggestion_error=why)
    filename = export_filename(result.target.ticker, result.as_of)
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/peers/{ticker}",
    response_model=PeersResponse,
    responses={**_ERRORS, 501: {"model": ErrorResponse, "description": "Source cannot screen for peers"}},
)
def get_peers(ticker: TickerPath, provider: ProviderDep, proxy_source: ProxySourceDep, today: TodayDep) -> PeersResponse:
    """Candidate peers from two sources combined: the peer group
    disclosed in the target's latest proxy statement (DEF 14A), filtered
    for business comparability (sector, market cap, EBITDA margin), and
    a screen on the target's industry (same currencies). The market-cap
    band, 0.2x-5.0x the target's, applies to both. Each name carries
    the source(s) that suggested it; names in both come first. Neither
    source is a peer set on its own, so curate the list. proxy_label and
    screen_label describe each source; flags explain every unreadable
    filing, unmapped or filtered proxy name, and thin result."""
    return suggest_peers(provider, ticker, proxy_source=proxy_source, today=today)
