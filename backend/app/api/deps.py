"""Request-scoped access to what the app factory put on app.state."""

from datetime import date
from typing import Annotated, Callable

from fastapi import Depends, Request

from app.data.provider import MarketDataProvider
from app.data.proxy_peers import ProxyPeerSource


def get_provider(request: Request) -> MarketDataProvider:
    provider = getattr(request.app.state, "provider", None)
    if provider is None:
        raise RuntimeError("market data provider not initialised; the app was started without its lifespan")
    return provider


def get_proxy_source(request: Request) -> ProxyPeerSource | None:
    """None when the app was built without one: peer suggestions then
    skip the proxy step and screen straight away."""
    return getattr(request.app.state, "proxy_source", None)


def get_today(request: Request) -> date:
    clock: Callable[[], date] = request.app.state.today
    return clock()


ProviderDep = Annotated[MarketDataProvider, Depends(get_provider)]
ProxySourceDep = Annotated[ProxyPeerSource | None, Depends(get_proxy_source)]
TodayDep = Annotated[date, Depends(get_today)]
