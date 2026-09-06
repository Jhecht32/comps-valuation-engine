"""CachedProvider: a TTL cache that wraps any MarketDataProvider.

Prices go stale in minutes and financial statements in months, so each
cached ticker carries two clocks. Inside the price TTL a call is served
from memory; past it the price alone is refreshed through the inner
provider's get_quote and spliced into the stored snapshot; past the
financials TTL the whole snapshot is refetched. Errors are never cached.

Entries are replaced, never mutated, and callers receive deep copies, so
concurrent readers in a thread pool cannot see a half-updated snapshot.
Two threads racing on the same cold ticker may both fetch it; the last
write wins and both get correct data.
"""

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from app.data.provider import (
    MarketDataError,
    MarketDataProvider,
    Quote,
    raise_for_currency_mismatch,
)
from app.valuation.models import CompanySnapshot


@dataclass(frozen=True)
class _Entry:
    snapshot: CompanySnapshot
    financials_at: float
    price_at: float


def _quote_of(snapshot: CompanySnapshot) -> Quote:
    return Quote(
        ticker=snapshot.ticker,
        share_price=snapshot.share_price,
        price_as_of=snapshot.price_as_of,
        price_currency=snapshot.price_currency,
    )


class CachedProvider(MarketDataProvider):
    def __init__(
        self,
        inner: MarketDataProvider,
        *,
        price_ttl: timedelta = timedelta(minutes=15),
        financials_ttl: timedelta = timedelta(hours=24),
        clock: Callable[[], float] = time.monotonic,
    ):
        self._inner = inner
        self.price_ttl = price_ttl
        self.financials_ttl = financials_ttl
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    # --- public API ---------------------------------------------------

    def get_company(self, ticker: str) -> CompanySnapshot:
        key = _key(ticker)
        entry = self._fresh_entry(key)
        if entry is None:
            entry = self._store(key, self._inner.get_company(key))
        elif self._price_expired(entry):
            entry = self._refresh_price(key, entry)
        return entry.snapshot.model_copy(deep=True)

    def get_companies(self, tickers: list[str]) -> dict[str, CompanySnapshot | MarketDataError]:
        misses: list[str] = []
        for ticker in tickers:
            key = _key(ticker)
            if self._fresh_entry(key) is None and key not in misses:
                misses.append(key)

        fetched = self._inner.get_companies(misses) if misses else {}
        for key, item in fetched.items():
            if isinstance(item, CompanySnapshot):
                self._store(key, item)

        results: dict[str, CompanySnapshot | MarketDataError] = {}
        for ticker in tickers:
            key = _key(ticker)
            entry = self._fresh_entry(key)
            if entry is None:
                results[ticker] = fetched.get(key) or MarketDataError(
                    f"{key}: provider returned no result"
                )
                continue
            try:
                if self._price_expired(entry):
                    entry = self._refresh_price(key, entry)
            except MarketDataError as err:
                results[ticker] = err
                continue
            results[ticker] = entry.snapshot.model_copy(deep=True)
        return results

    def get_quote(self, ticker: str) -> Quote:
        key = _key(ticker)
        entry = self._fresh_entry(key)
        if entry is None:
            # A bare quote does not populate the cache: there is no
            # snapshot to attach it to.
            return self._inner.get_quote(key)
        if self._price_expired(entry):
            entry = self._refresh_price(key, entry)
        return _quote_of(entry.snapshot)

    def fetch_company(self, ticker: str) -> CompanySnapshot:
        return self._inner.get_company(ticker)

    def clear(self) -> None:
        self._entries.clear()

    # --- internals ----------------------------------------------------

    def _fresh_entry(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.financials_at > self.financials_ttl.total_seconds():
            self._entries.pop(key, None)
            return None
        return entry

    def _price_expired(self, entry: _Entry) -> bool:
        return self._clock() - entry.price_at > self.price_ttl.total_seconds()

    def _store(self, key: str, snapshot: CompanySnapshot) -> _Entry:
        now = self._clock()
        entry = _Entry(snapshot=snapshot, financials_at=now, price_at=now)
        self._entries[key] = entry
        return entry

    def _refresh_price(self, key: str, entry: _Entry) -> _Entry:
        quote = self._inner.get_quote(key)
        snapshot = entry.snapshot.model_copy(
            update={
                "share_price": quote.share_price,
                "price_as_of": quote.price_as_of,
                "price_currency": quote.price_currency,
            }
        )
        raise_for_currency_mismatch(snapshot)
        refreshed = _Entry(
            snapshot=snapshot, financials_at=entry.financials_at, price_at=self._clock()
        )
        self._entries[key] = refreshed
        return refreshed


def _key(ticker: str) -> str:
    return ticker.strip().upper()
