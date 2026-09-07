"""Service-level errors and the mapping from every failure class to the
TickerError shape the API returns."""

from app.data.provider import (
    CurrencyMismatchError,
    MarketDataError,
    PeerDiscoveryUnavailableError,
    TickerNotFoundError,
)
from app.services.models import ErrorCode, TickerError


class InsufficientDataError(Exception):
    """The source returned a snapshot the engine refuses to value: too few
    quarters for an LTM window, no share count, a non-positive price.
    Distinct from MarketDataError, which means the source itself failed."""

    def __init__(self, ticker: str, detail: str):
        self.ticker = ticker
        self.detail = detail
        super().__init__(f"{ticker}: {detail}")


def error_code_for(exc: Exception) -> ErrorCode:
    if isinstance(exc, TickerNotFoundError):
        return ErrorCode.TICKER_NOT_FOUND
    if isinstance(exc, CurrencyMismatchError):
        return ErrorCode.CURRENCY_MISMATCH
    if isinstance(exc, PeerDiscoveryUnavailableError):
        return ErrorCode.PEER_DISCOVERY_UNAVAILABLE
    if isinstance(exc, MarketDataError):
        return ErrorCode.SOURCE_ERROR
    if isinstance(exc, InsufficientDataError):
        return ErrorCode.INSUFFICIENT_DATA
    raise TypeError(f"not a per-ticker failure: {exc!r}")


def ticker_error(ticker: str | None, exc: Exception) -> TickerError:
    return TickerError(ticker=ticker, code=error_code_for(exc), message=str(exc))
