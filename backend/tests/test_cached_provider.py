"""Tests for CachedProvider, the TTL cache that wraps any provider.

Prices go stale in minutes, financial statements in months, so the cache
keeps two clocks per ticker: after the price TTL it refreshes only the
quote through get_quote, after the financials TTL it refetches the whole
snapshot. Errors are never cached.
"""

from datetime import date, timedelta

import pytest

from app.data import CachedProvider, TickerNotFoundError
from app.data.fixtures import HOME_DEPOT
from app.data.provider import CurrencyMismatchError, Quote
from tests.helpers import CountingProvider

LOWES = HOME_DEPOT.model_copy(update={"ticker": "LOW", "name": "Lowe's"})


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def advance(self, delta: timedelta):
        self.now += delta.total_seconds()


@pytest.fixture
def env():
    inner = CountingProvider({"HD": HOME_DEPOT, "LOW": LOWES})
    clock = FakeClock()
    cached = CachedProvider(
        inner,
        price_ttl=timedelta(minutes=15),
        financials_ttl=timedelta(hours=24),
        clock=clock,
    )
    return inner, clock, cached


def new_quote(price, as_of=date(2026, 9, 8), currency="USD"):
    return Quote(ticker="HD", share_price=price, price_as_of=as_of, price_currency=currency)


def test_defaults_are_15_minutes_and_24_hours():
    cached = CachedProvider(CountingProvider({}))
    assert cached.price_ttl == timedelta(minutes=15)
    assert cached.financials_ttl == timedelta(hours=24)


def test_repeat_call_within_price_ttl_does_not_touch_the_source(env):
    inner, clock, cached = env
    cached.get_company("HD")
    clock.advance(timedelta(minutes=14))
    cached.get_company("HD")
    assert inner.fetch_calls == ["HD"]
    assert inner.quote_calls == []


def test_price_refresh_after_price_ttl_uses_only_a_quote(env):
    inner, clock, cached = env
    cached.get_company("HD")
    inner.quotes["HD"] = new_quote(330.0)
    clock.advance(timedelta(minutes=16))

    snapshot = cached.get_company("HD")

    assert snapshot.share_price == 330.0
    assert snapshot.price_as_of == date(2026, 9, 8)
    assert snapshot.quarters == HOME_DEPOT.quarters  # financials untouched
    assert inner.fetch_calls == ["HD"]
    assert inner.quote_calls == ["HD"]


def test_refreshed_price_is_served_from_cache_until_it_expires_again(env):
    inner, clock, cached = env
    cached.get_company("HD")
    inner.quotes["HD"] = new_quote(330.0)
    clock.advance(timedelta(minutes=16))
    cached.get_company("HD")
    clock.advance(timedelta(minutes=14))
    assert cached.get_company("HD").share_price == 330.0
    assert inner.quote_calls == ["HD"]


def test_full_refetch_after_financials_ttl(env):
    inner, clock, cached = env
    cached.get_company("HD")
    clock.advance(timedelta(hours=24, minutes=1))
    cached.get_company("HD")
    assert inner.fetch_calls == ["HD", "HD"]
    assert inner.quote_calls == []


def test_errors_are_not_cached():
    inner = CountingProvider({})
    cached = CachedProvider(inner, clock=FakeClock())
    with pytest.raises(TickerNotFoundError):
        cached.get_company("HD")
    inner.snapshots["HD"] = HOME_DEPOT
    assert cached.get_company("HD").ticker == "HD"


def test_cached_snapshot_is_a_copy(env):
    _, _, cached = env
    first = cached.get_company("HD")
    first.quarters.clear()
    assert len(cached.get_company("HD").quarters) == 4


def test_cache_key_is_normalized(env):
    inner, _, cached = env
    cached.get_company("hd")
    cached.get_company(" HD ")
    assert inner.fetch_calls == ["HD"]


def test_refreshed_quote_in_another_currency_is_rejected(env):
    inner, clock, cached = env
    cached.get_company("HD")
    inner.quotes["HD"] = new_quote(330.0, currency="CAD")
    clock.advance(timedelta(minutes=16))
    with pytest.raises(CurrencyMismatchError):
        cached.get_company("HD")


def test_get_quote_is_cached_on_the_same_clock(env):
    inner, clock, cached = env
    cached.get_company("HD")
    assert cached.get_quote("HD").share_price == 321.05
    assert inner.quote_calls == []
    inner.quotes["HD"] = new_quote(330.0)
    clock.advance(timedelta(minutes=16))
    assert cached.get_quote("HD").share_price == 330.0
    assert inner.quote_calls == ["HD"]


def test_get_companies_fetches_only_misses_through_the_inner_batch(env):
    inner, _, cached = env
    cached.get_company("HD")

    results = cached.get_companies(["HD", "LOW", "NOPE"])

    assert inner.batch_calls == [["LOW", "NOPE"]]
    assert list(results) == ["HD", "LOW", "NOPE"]
    assert results["HD"].ticker == "HD"
    assert results["LOW"].ticker == "LOW"
    assert isinstance(results["NOPE"], TickerNotFoundError)


def test_get_companies_serves_everything_from_cache_when_fresh(env):
    inner, _, cached = env
    cached.get_companies(["HD", "LOW"])
    cached.get_companies(["HD", "LOW"])
    assert inner.batch_calls == [["HD", "LOW"]]


def test_get_companies_refreshes_stale_prices_with_quotes(env):
    inner, clock, cached = env
    cached.get_company("HD")
    inner.quotes["HD"] = new_quote(330.0)
    clock.advance(timedelta(minutes=16))
    results = cached.get_companies(["HD"])
    assert results["HD"].share_price == 330.0
    assert inner.batch_calls == []
    assert inner.quote_calls == ["HD"]


def test_clear_forces_a_refetch(env):
    inner, _, cached = env
    cached.get_company("HD")
    cached.clear()
    cached.get_company("HD")
    assert inner.fetch_calls == ["HD", "HD"]
