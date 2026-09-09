"""FastAPI application factory.

    uvicorn app.main:app --reload

create_app takes the inner market data provider (yfinance by default),
a proxy peer source (SEC EDGAR by default, but only when the provider
is also defaulted, so a test that injects a provider never reaches the
network; set COMPS_PROXY_PEERS=off to run without it), and a clock; at
startup the lifespan wraps the provider in CachedProvider so every
request shares one TTL cache, and exposes both on app.state. Tests pass
a fixture provider, a fixture proxy source, and a fixed date.

The single-file frontend in ../frontend is served at / from the same
app, so no separate static server (and no CORS) is needed locally or in
a deployment. The directory is found relative to this file, whatever
the working directory, or set explicitly with COMPS_FRONTEND_DIR (the
deployed app, installed into site-packages, uses that); a directory
that does not exist is skipped with a warning.

Deployed with uvicorn's factory mode against create_app:

    uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.data import CachedProvider, MarketDataProvider
from app.data.provider import (
    CurrencyMismatchError,
    MarketDataError,
    PeerDiscoveryUnavailableError,
    TickerNotFoundError,
)
from app.data.proxy_peers import ProxyPeerSource
from app.services import ErrorCode, InsufficientDataError, TickerError, ticker_error

log = logging.getLogger(__name__)

DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
PROXY_PEERS_ENV = "COMPS_PROXY_PEERS"
FRONTEND_DIR_ENV = "COMPS_FRONTEND_DIR"
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

# Sentinel default for create_app's frontend_dir: resolve from the
# environment when the app is built, so a deployment can point at its
# own copy of the frontend and None still means "no frontend".
_FROM_ENV = object()

# Most specific first; Starlette matches handlers by the exception's MRO.
_STATUS_BY_ERROR: list[tuple[type[Exception], int]] = [
    (TickerNotFoundError, 404),
    (CurrencyMismatchError, 422),
    (InsufficientDataError, 422),
    (PeerDiscoveryUnavailableError, 501),
    (MarketDataError, 502),
]


def _default_provider() -> MarketDataProvider:
    from app.data.yfinance_provider import YFinanceProvider  # pulls in pandas

    return YFinanceProvider()


def _default_proxy_source() -> ProxyPeerSource | None:
    if os.environ.get(PROXY_PEERS_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
        return None
    from app.data.edgar import EdgarProxyPeerSource

    return EdgarProxyPeerSource()


def _frontend_dir() -> Path:
    """COMPS_FRONTEND_DIR when set (a relative path is taken from the
    working directory at startup), else the checkout's ../frontend."""
    raw = os.environ.get(FRONTEND_DIR_ENV, "").strip()
    return Path(raw).expanduser().resolve() if raw else FRONTEND_DIR


def _cors_origins() -> list[str]:
    raw = os.environ.get("COMPS_CORS_ORIGINS")
    return [o.strip() for o in raw.split(",") if o.strip()] if raw else DEFAULT_CORS_ORIGINS


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"


def _register_error_handlers(app: FastAPI) -> None:
    def make_handler(status: int):
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            ticker = getattr(exc, "ticker", None) or request.path_params.get("ticker")
            body = ticker_error(ticker.strip().upper() if ticker else None, exc)
            return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

        return handler

    for error_type, status in _STATUS_BY_ERROR:
        app.add_exception_handler(error_type, make_handler(status))

    # Request validation gets the same body shape as every other error;
    # the routes declare 422 as TickerError and this keeps that true.
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        body = TickerError(ticker=None, code=ErrorCode.INVALID_REQUEST, message=_validation_message(exc))
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    app.add_exception_handler(RequestValidationError, validation_handler)


def create_app(
    *,
    provider: MarketDataProvider | None = None,
    proxy_source: ProxyPeerSource | None = None,
    today: Callable[[], date] = date.today,
    cors_origins: list[str] | None = None,
    frontend_dir: Path | None | object = _FROM_ENV,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The default provider is built here, not at import, so importing
        # app.main (tests, tooling) does not construct a yfinance client.
        inner = provider if provider is not None else _default_provider()
        app.state.inner_provider = inner
        app.state.provider = CachedProvider(inner)
        # EDGAR is the default only alongside the default provider: an
        # injected provider with no proxy source means "offline".
        app.state.proxy_source = (
            proxy_source if proxy_source is not None else (_default_proxy_source() if provider is None else None)
        )
        yield

    app = FastAPI(
        title="Comps Valuation Engine",
        version="0.1.0",
        description="Public comparable companies analysis: EV bridge, LTM multiples, implied valuation.",
        lifespan=lifespan,
    )
    app.state.today = today
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else _cors_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(router)
    _register_error_handlers(app)
    if frontend_dir is _FROM_ENV:
        frontend_dir = _frontend_dir()
    if isinstance(frontend_dir, Path):
        if frontend_dir.is_dir():
            # Mounted last so the /api routes match first; html=True serves
            # index.html at /.
            app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
        else:
            # Backend-only: the API still works, / is a 404.
            log.warning("frontend directory %s not found; serving the API only", frontend_dir)
    return app


app = create_app()
