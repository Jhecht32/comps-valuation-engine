"""MarketDataProvider: the boundary between external data and the engine.

A provider fetches one company's data from one source and maps it into a
CompanySnapshot. Mapping is the whole job. Anything that requires
judgment about the numbers (LTM stitching, fallbacks, flags, multiples)
belongs to the valuation module, which stays network-free; anything that
requires knowing the source's field names belongs in the provider.

Mapping rules every implementation must honour:

- Raw dollars, never millions; shares as a raw count.
- None for a line the source does not report or break out, 0.0 for a
  line the source reports as zero. The engine's *_reported booleans and
  the EPS_FROM_CONSOLIDATED_NI flag are derived from that distinction.
- nci_income is income attributable to noncontrolling interests from the
  income statement; noncontrolling_interest is the balance sheet equity
  line. They have similar names in most sources and must not be crossed.
- d_and_a comes from the cash flow statement. When a filer splits it into
  depreciation and intangible amortization lines, sum them.
- Periods newest first.
- price_currency and reporting_currency filled in whenever the source
  knows them; the base class refuses snapshots where they differ or are
  unknown (see raise_for_currency_mismatch).

Implementations override fetch_company (and optionally get_quote for a
cheap price-only path, and get_companies for a multi-ticker fetch). The
public get_company / get_companies entry points apply source-independent
guards on top. Implementations are synchronous; the API layer runs them
in a worker thread if it needs to stay non-blocking.
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import Mapping

from pydantic import BaseModel

from app.valuation.models import CompanySnapshot


# --- Errors ---------------------------------------------------------------


class MarketDataError(Exception):
    """The source could not supply usable data (transport failure, no
    price, malformed statements). Message explains what was missing."""


class TickerNotFoundError(MarketDataError):
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"No market data found for ticker {ticker!r}")


class CurrencyMismatchError(MarketDataError):
    """The company cannot be valued in a single currency.

    Either its share price and its financial statements are in different
    currencies (ADRs, dual listings), or it reports in a different
    currency from the comps target. Both make equity value, and every
    multiple built on it, arithmetically fine and economically
    meaningless without an FX conversion the engine deliberately does
    not do.
    """

    def __init__(
        self,
        ticker: str,
        detail: str,
        *,
        price_currency: str | None = None,
        reporting_currency: str | None = None,
        target_currency: str | None = None,
    ):
        self.ticker = ticker
        self.price_currency = price_currency
        self.reporting_currency = reporting_currency
        self.target_currency = target_currency
        super().__init__(f"{ticker}: {detail}")


# --- Quote ----------------------------------------------------------------


class Quote(BaseModel):
    """A price-only observation, for refreshing a cached snapshot."""

    ticker: str
    share_price: float
    price_as_of: date
    price_currency: str | None = None


# --- Currency guards ------------------------------------------------------


def raise_for_currency_mismatch(snapshot: CompanySnapshot) -> None:
    """Reject a snapshot whose price and financials are not confirmed to
    be in the same currency. Unknown counts as unconfirmed: a snapshot
    that cannot be checked is not let through on the hope it is fine."""
    price, reporting = snapshot.price_currency, snapshot.reporting_currency
    if price is None or reporting is None:
        raise CurrencyMismatchError(
            snapshot.ticker,
            f"currency could not be confirmed (price currency {price!r}, reporting "
            f"currency {reporting!r}); refusing to value a company whose currencies "
            "cannot be checked",
            price_currency=price,
            reporting_currency=reporting,
        )
    if price != reporting:
        raise CurrencyMismatchError(
            snapshot.ticker,
            f"share price is quoted in {price} but financials are reported in "
            f"{reporting}; equity value would mix currencies",
            price_currency=price,
            reporting_currency=reporting,
        )


def reject_currency_mismatches(
    target: CompanySnapshot,
    peers: Mapping[str, CompanySnapshot | MarketDataError],
) -> dict[str, CompanySnapshot | MarketDataError]:
    """Replace every peer reporting in a currency other than the target's
    with a CurrencyMismatchError, in the same per-ticker shape that
    get_companies returns. Existing errors pass through untouched."""
    kept: dict[str, CompanySnapshot | MarketDataError] = {}
    for key, item in peers.items():
        if isinstance(item, CompanySnapshot) and item.reporting_currency != target.reporting_currency:
            item = CurrencyMismatchError(
                item.ticker,
                f"reports in {item.reporting_currency} but the target {target.ticker} reports "
                f"in {target.reporting_currency}; peers must share the target's currency",
                reporting_currency=item.reporting_currency,
                target_currency=target.reporting_currency,
            )
        kept[key] = item
    return kept


# --- Interface ------------------------------------------------------------


class MarketDataProvider(ABC):
    def get_company(self, ticker: str) -> CompanySnapshot:
        """Fetch and map one company, then apply the currency guard.

        Raises TickerNotFoundError when the source has never heard of the
        ticker, CurrencyMismatchError when the snapshot cannot be valued
        in one currency, and MarketDataError when the source returned
        nothing the engine can price (no share price, no statements at
        all). Partial data is not an error: missing lines come back as
        None and the engine flags them.
        """
        snapshot = self.fetch_company(ticker)
        raise_for_currency_mismatch(snapshot)
        return snapshot

    def get_companies(self, tickers: list[str]) -> dict[str, CompanySnapshot | MarketDataError]:
        """Fetch several companies, keyed by ticker as given, in order.

        Per-ticker failures are returned as the MarketDataError instance
        rather than raised, so one bad peer cannot kill a comps run.
        Anything that is not a MarketDataError is a bug and propagates.
        Sources with a multi-ticker endpoint override this.
        """
        results: dict[str, CompanySnapshot | MarketDataError] = {}
        for ticker in tickers:
            try:
                results[ticker] = self.get_company(ticker)
            except MarketDataError as err:
                results[ticker] = err
        return results

    def get_quote(self, ticker: str) -> Quote:
        """Current price only. The default does a full fetch; sources with
        a cheap price endpoint override it so caches can refresh prices
        without re-pulling statements."""
        snapshot = self.get_company(ticker)
        return Quote(
            ticker=snapshot.ticker,
            share_price=snapshot.share_price,
            price_as_of=snapshot.price_as_of,
            price_currency=snapshot.price_currency,
        )

    @abstractmethod
    def fetch_company(self, ticker: str) -> CompanySnapshot:
        """Source-specific fetch and map. Called by get_company; do not
        call directly, it bypasses the guards."""
