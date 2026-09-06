"""Tests for the source-independent behaviour of MarketDataProvider:
the currency guard, per-ticker error handling in the batch method, and
the default quote.

The guard exists because yfinance (and most sources) quote ADRs and
dual-listed names in the listing currency while reporting financials in
the home currency; the resulting equity value is arithmetically fine and
economically meaningless, and nothing downstream can catch it.
"""

from datetime import date

import pytest

from app.data import FixtureProvider, MarketDataError, MarketDataProvider, TickerNotFoundError
from app.data.fixtures import HOME_DEPOT
from app.data.provider import CurrencyMismatchError, Quote, reject_currency_mismatches


def snapshot_like_hd(ticker, price_currency, reporting_currency):
    return HOME_DEPOT.model_copy(
        update={
            "ticker": ticker,
            "price_currency": price_currency,
            "reporting_currency": reporting_currency,
        }
    )


# --- Per-company guard: price currency must equal reporting currency -------


def test_price_vs_reporting_currency_mismatch_is_rejected():
    provider = FixtureProvider(snapshots={"TSM": snapshot_like_hd("TSM", "USD", "TWD")})
    with pytest.raises(CurrencyMismatchError) as excinfo:
        provider.get_company("TSM")
    err = excinfo.value
    assert isinstance(err, MarketDataError)
    assert err.ticker == "TSM"
    assert err.price_currency == "USD"
    assert err.reporting_currency == "TWD"
    assert "USD" in str(err) and "TWD" in str(err)


def test_unknown_currency_cannot_pass_the_guard():
    provider = FixtureProvider(snapshots={"X": snapshot_like_hd("X", "USD", None)})
    with pytest.raises(CurrencyMismatchError):
        provider.get_company("X")


def test_matching_currencies_pass():
    snapshot = FixtureProvider().get_company("HD")
    assert snapshot.price_currency == "USD"
    assert snapshot.reporting_currency == "USD"


# --- Peer-set guard: every peer must share the target's currency ---------


def test_peers_in_another_currency_are_replaced_with_errors():
    target = FixtureProvider().get_company("HD")
    peers = {
        "LOW": snapshot_like_hd("LOW", "USD", "USD"),
        "ADEN.SW": snapshot_like_hd("ADEN.SW", "CHF", "CHF"),
        "NOPE": TickerNotFoundError("NOPE"),
    }
    kept = reject_currency_mismatches(target, peers)
    assert list(kept) == ["LOW", "ADEN.SW", "NOPE"]
    assert kept["LOW"] is peers["LOW"]
    assert isinstance(kept["ADEN.SW"], CurrencyMismatchError)
    assert kept["ADEN.SW"].ticker == "ADEN.SW"
    assert "CHF" in str(kept["ADEN.SW"]) and "USD" in str(kept["ADEN.SW"])
    assert kept["NOPE"] is peers["NOPE"]  # existing errors pass through untouched


def test_peer_check_does_not_mutate_input():
    target = FixtureProvider().get_company("HD")
    peers = {"ADEN.SW": snapshot_like_hd("ADEN.SW", "CHF", "CHF")}
    reject_currency_mismatches(target, peers)
    assert not isinstance(peers["ADEN.SW"], MarketDataError)


# --- Batch method ---------------------------------------------------------


def test_get_companies_returns_errors_per_ticker_in_input_order():
    provider = FixtureProvider(
        snapshots={"HD": HOME_DEPOT, "TSM": snapshot_like_hd("TSM", "USD", "TWD")}
    )
    results = provider.get_companies(["HD", "NOPE", "TSM"])
    assert list(results) == ["HD", "NOPE", "TSM"]
    assert results["HD"].ticker == "HD"
    assert isinstance(results["NOPE"], TickerNotFoundError)
    assert isinstance(results["TSM"], CurrencyMismatchError)


class ExplodingProvider(MarketDataProvider):
    def fetch_company(self, ticker):
        raise RuntimeError("bug in the provider")


def test_get_companies_propagates_programming_errors():
    with pytest.raises(RuntimeError):
        ExplodingProvider().get_companies(["HD"])


# --- Default quote --------------------------------------------------------


def test_default_quote_is_the_snapshot_price():
    quote = FixtureProvider().get_quote("HD")
    assert quote == Quote(
        ticker="HD", share_price=321.05, price_as_of=date(2026, 9, 4), price_currency="USD"
    )
