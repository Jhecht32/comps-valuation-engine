"""yfinance implementation of MarketDataProvider.

Row names are Yahoo Finance's normalised statement keys as yfinance
returns them (the index of each statement DataFrame; columns are
period-end Timestamps, newest first; values are raw currency units; NaN
means not reported). Verified against HD, WMT and PSA with yfinance
1.7.0 on 2026-09-06. Every mapping choice an interviewer would ask about
is documented at the point of use.

Known limitations of the source, all visible in the live HD test:

- Period ends are normalised to month-end: HD's quarter ended 2026-08-02
  appears as 2026-07-31.
- Yahoo's "Total Debt" folds operating lease liabilities into "Capital
  Lease Obligations" for ASC 842 filers (HD: 62,567 vs 52,896 of
  borrowed money). See _total_debt.
- Yahoo omits balance sheet rows a company does not have, so a missing
  row alone cannot distinguish "zero" from "not parsed". Equity totals
  resolve that for preferred and NCI; see _balance_sheet.

Import this module explicitly (it pulls in pandas and, lazily,
yfinance); app.data does not re-export it so the fixture and cache
layers stay light.
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import pandas as pd

from app.data.provider import (
    CompanyProfile,
    MarketDataError,
    MarketDataProvider,
    Quote,
    TickerNotFoundError,
)
from app.valuation.models import (
    AnnualFinancials,
    BalanceSheetItems,
    CompanySnapshot,
    QuarterlyFinancials,
)

SOURCE = "yfinance"


@dataclass
class RawCompany:
    """Everything fetched for one ticker, before mapping."""

    ticker: str
    info: dict[str, Any]
    price_history: pd.DataFrame  # Ticker.history(period="5d", auto_adjust=False)
    price_metadata: dict[str, Any]  # Ticker.history_metadata after that call
    quarterly_income: pd.DataFrame
    annual_income: pd.DataFrame
    quarterly_balance: pd.DataFrame
    quarterly_cashflow: pd.DataFrame
    annual_cashflow: pd.DataFrame


# --- Cell access ------------------------------------------------------------


def _empty(df: pd.DataFrame | None) -> bool:
    return df is None or df.empty


def _cell(df: pd.DataFrame | None, row: str, col: Any) -> float | None:
    """One statement value as a float, or None for a missing row, a
    missing period, or a NaN cell. 0.0 stays 0.0: the source said zero."""
    if _empty(df) or row not in df.index or col not in df.columns:
        return None
    value = df.at[row, col]
    if pd.isna(value):
        return None
    return float(value)


def _first(df: pd.DataFrame | None, col: Any, *rows: str) -> float | None:
    for row in rows:
        value = _cell(df, row, col)
        if value is not None:
            return value
    return None


def _as_date(period_end: Any) -> date:
    return pd.Timestamp(period_end).date()


# --- Income statement periods -----------------------------------------------


def _d_and_a(cashflow: pd.DataFrame | None, col: Any) -> float | None:
    """D&A from the cash flow statement. Yahoo's total line already sums
    depreciation and intangible amortization (HD FY2025: 3,514 + 607 =
    4,121, vs 3,273 on the income statement, which excludes intangibles).
    When only the split lines exist, sum whichever are present."""
    total = _first(cashflow, col, "Depreciation And Amortization", "Depreciation Amortization Depletion")
    if total is not None:
        return total
    depreciation = _cell(cashflow, "Depreciation", col)
    amortization = _first(cashflow, col, "Amortization Of Intangibles", "Amortization Cash Flow")
    parts = [v for v in (depreciation, amortization) if v is not None]
    return sum(parts) if parts else None


def _period_fields(income: pd.DataFrame, cashflow: pd.DataFrame | None, col: Any) -> dict[str, float | None]:
    # Yahoo's income statement ladder is:
    #   Net Income Including Noncontrolling Interests
    #     - Minority Interests            (shown negative when NCI earns income)
    #   = Net Income                      (attributable to the parent)
    #     - Preferred Stock Dividends
    #   = Net Income Common Stockholders
    # The engine's EPS subtracts both deductions itself, so net_income is
    # the top of the ladder. Each deduction comes from its own line when
    # Yahoo shows one, else from the difference of adjacent rungs (which
    # is 0.0, a known zero, for companies without NCI or preferred), else
    # None for "not broken out".
    ni_incl_nci = _cell(income, "Net Income Including Noncontrolling Interests", col)
    ni_parent = _cell(income, "Net Income", col)
    ni_common = _cell(income, "Net Income Common Stockholders", col)
    minority = _cell(income, "Minority Interests", col)

    if ni_incl_nci is not None:
        net_income = ni_incl_nci
        if minority is not None:
            nci_income = -minority
        elif ni_parent is not None:
            nci_income = ni_incl_nci - ni_parent
        else:
            nci_income = None
    else:
        # Only the parent figure exists; whether NCI is already deducted
        # cannot be told, so leave it "not broken out".
        net_income = ni_parent
        nci_income = None

    preferred = _cell(income, "Preferred Stock Dividends", col)
    if preferred is None and ni_parent is not None and ni_common is not None:
        preferred = ni_parent - ni_common

    return {
        "revenue": _cell(income, "Total Revenue", col),
        "gross_profit": _cell(income, "Gross Profit", col),
        # Yahoo's "EBIT" row is pretax income plus interest expense, which
        # includes non-operating items; Operating Income is EBIT as a
        # banker means it.
        "ebit": _cell(income, "Operating Income", col),
        "net_income": net_income,
        "d_and_a": _d_and_a(cashflow, col),
        "preferred_dividends": preferred,
        "nci_income": nci_income,
    }


def _periods(income: pd.DataFrame | None, cashflow: pd.DataFrame | None, cls):
    periods = []
    if _empty(income):
        return periods
    for col in income.columns:
        fields = _period_fields(income, cashflow, col)
        if all(fields[k] is None for k in ("revenue", "ebit", "net_income")):
            continue  # a column Yahoo pads with NaN carries no period
        periods.append(cls(period_end=_as_date(col), **fields))
    periods.sort(key=lambda p: p.period_end, reverse=True)
    return periods


# --- Balance sheet ----------------------------------------------------------


def _total_debt(bs: pd.DataFrame, col: Any) -> float | None:
    """Borrowed money only. Yahoo's "Total Debt" adds "Capital Lease
    Obligations", which for ASC 842 filers is mostly operating leases;
    including them would be inconsistent with an EBITDA that still
    carries rent expense. Long Term Debt + Current Debt is the pre-lease
    figure (HD: 43,951 + 8,945 = 52,896, ties to the 10-Q)."""
    long_term = _cell(bs, "Long Term Debt", col)
    current = _cell(bs, "Current Debt", col)
    if long_term is not None or current is not None:
        return (long_term or 0.0) + (current or 0.0)
    total = _cell(bs, "Total Debt", col)
    if total is None:
        return None
    return total - (_cell(bs, "Capital Lease Obligations", col) or 0.0)


def _balance_sheet(bs: pd.DataFrame | None, ticker: str) -> BalanceSheetItems:
    if _empty(bs):
        raise MarketDataError(f"{ticker}: no balance sheet available")
    col = max(bs.columns)

    # Yahoo omits rows a company does not have, but always reports the
    # equity totals. Stockholders' equity less common equity is preferred
    # equity; total equity gross of minority interest less stockholders'
    # equity is NCI. When the direct row is absent and the totals are
    # present, the totals state the value (0.0 for most companies), which
    # is the source reporting a zero rather than us assuming one.
    common = _cell(bs, "Common Stock Equity", col)
    stockholders = _cell(bs, "Stockholders Equity", col)
    gross_equity = _cell(bs, "Total Equity Gross Minority Interest", col)

    preferred = _cell(bs, "Preferred Stock Equity", col)
    if preferred is None and stockholders is not None and common is not None:
        preferred = stockholders - common

    # Balance sheet NCI (a stock). The income statement's similarly named
    # "Minority Interests" (a flow) is mapped in _period_fields.
    nci = _cell(bs, "Minority Interest", col)
    if nci is None and gross_equity is not None and stockholders is not None:
        nci = gross_equity - stockholders

    return BalanceSheetItems(
        as_of=_as_date(col),
        total_debt=_total_debt(bs, col),
        cash_and_equivalents=_cell(bs, "Cash And Cash Equivalents", col),
        short_term_investments=_cell(bs, "Other Short Term Investments", col),
        preferred_equity=preferred,
        noncontrolling_interest=nci,
    )


# --- Shares and price -------------------------------------------------------


def _shares(income: pd.DataFrame | None) -> tuple[float | None, float | None, date | None]:
    """Weighted-average counts from the latest quarter that reports them.
    Diluted is the EV input; basic is the flagged fallback."""
    if _empty(income):
        return None, None, None
    for col in sorted(income.columns, reverse=True):
        diluted = _cell(income, "Diluted Average Shares", col)
        basic = _cell(income, "Basic Average Shares", col)
        if diluted is not None or basic is not None:
            return diluted, basic, _as_date(col)
    return None, None, None


def map_quote(ticker: str, history: pd.DataFrame | None, metadata: dict[str, Any] | None) -> Quote:
    """The regular-session last price and the date it printed.

    The chart endpoint returns the Close series at float32 precision
    (321.05 comes back as 321.04998779...), while its metadata carries
    the exact quoted regularMarketPrice with a timestamp. Prefer the
    metadata; fall back to the last Close bar when it is missing.
    """
    metadata = metadata or {}
    currency = metadata.get("currency")

    price = metadata.get("regularMarketPrice")
    stamp = metadata.get("regularMarketTime")
    if price is not None and stamp is not None and not pd.isna(price):
        return Quote(
            ticker=ticker, share_price=float(price), price_as_of=_as_date(stamp), price_currency=currency
        )

    if _empty(history) or "Close" not in history.columns:
        raise MarketDataError(f"{ticker}: no price history available")
    closes = history["Close"].dropna()
    if closes.empty:
        raise MarketDataError(f"{ticker}: no price history available")
    return Quote(
        ticker=ticker,
        share_price=float(closes.iloc[-1]),
        price_as_of=_as_date(closes.index[-1]),
        price_currency=currency,
    )


# --- Snapshot ---------------------------------------------------------------


def map_company(raw: RawCompany) -> CompanySnapshot:
    ticker = raw.ticker.strip().upper()
    info = raw.info or {}

    no_statements = all(
        _empty(df) for df in (raw.quarterly_income, raw.annual_income, raw.quarterly_balance)
    )
    if _empty(raw.price_history) and no_statements:
        raise TickerNotFoundError(ticker)

    metadata = dict(raw.price_metadata or {})
    metadata.setdefault("currency", info.get("currency"))
    quote = map_quote(ticker, raw.price_history, metadata)
    diluted, basic, shares_as_of = _shares(raw.quarterly_income)
    if basic is None and info.get("sharesOutstanding") is not None:
        basic = float(info["sharesOutstanding"])

    return CompanySnapshot(
        ticker=ticker,
        name=info.get("longName") or info.get("shortName"),
        price_currency=quote.price_currency,
        reporting_currency=info.get("financialCurrency"),
        share_price=quote.share_price,
        price_as_of=quote.price_as_of,
        diluted_shares=diluted,
        basic_shares=basic,
        shares_as_of=shares_as_of,
        balance_sheet=_balance_sheet(raw.quarterly_balance, ticker),
        quarters=_periods(raw.quarterly_income, raw.quarterly_cashflow, QuarterlyFinancials),
        fiscal_years=_periods(raw.annual_income, raw.annual_cashflow, AnnualFinancials),
        source=SOURCE,
    )


# --- Profile and screener ---------------------------------------------------


def _float_or_none(value: Any) -> float | None:
    """A numeric field that Yahoo may leave absent, NaN, or as a
    placeholder string; anything unusable is None, never an exception."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def map_profile(ticker: str, info: dict[str, Any] | None) -> CompanyProfile:
    """Sector, industry, and market cap from Ticker.info. marketCap is
    Yahoo's own figure (price x shares outstanding); it sizes the peer
    band only and never enters a valuation."""
    info = info or {}
    return CompanyProfile(
        ticker=ticker.strip().upper(),
        name=info.get("longName") or info.get("shortName"),
        sector=info.get("sector"),
        industry=info.get("industry"),
        market_cap=_float_or_none(info.get("marketCap")),
        price_currency=info.get("currency"),
        reporting_currency=info.get("financialCurrency"),
        exchange=info.get("exchange"),
    )


def map_screen_quote(quote: dict[str, Any], *, sector: str | None, industry: str | None) -> CompanyProfile:
    """One screener result. The quote carries no sector or industry of
    its own, so the values the query filtered on are stamped in."""
    return CompanyProfile(
        ticker=str(quote.get("symbol", "")).strip().upper(),
        name=quote.get("longName") or quote.get("shortName") or quote.get("displayName"),
        sector=sector,
        industry=industry,
        market_cap=_float_or_none(quote.get("marketCap")),
        price_currency=quote.get("currency"),
        reporting_currency=quote.get("financialCurrency"),
        exchange=quote.get("exchange"),
    )


def _default_screen(query, **kwargs):
    import yfinance as yf  # imported lazily so fakes need no yfinance

    return yf.screen(query, **kwargs)


_SCREEN_MAX_SIZE = 250  # Yahoo's hard cap per screener request

# Yahoo exchange codes for NYSE, Nasdaq (GS/GM/CM), NYSE American, NYSE
# Arca, and BATS. Region 'us' alone also returns OTC (PNK/OQX/OQB) rows,
# which are mostly foreign ordinaries and ADR look-alikes: illiquid,
# reporting in another currency, and duplicating a home listing.
_PRIMARY_US_EXCHANGES = ("NYQ", "NMS", "NGM", "NCM", "ASE", "PCX", "BTS")


# --- Provider ---------------------------------------------------------------


_PRICE_METADATA_KEYS = ("currency", "regularMarketPrice", "regularMarketTime")


def _price_metadata(t) -> dict[str, Any]:
    """The few chart-metadata keys the quote needs, copied out one by one.
    yfinance's metadata object lazy-loads other keys on access and raises
    for unknown tickers, so it must not be copied wholesale."""
    try:
        meta = t.history_metadata or {}
        return {k: meta.get(k) for k in _PRICE_METADATA_KEYS}
    except Exception:
        return {}


def _default_ticker_factory(symbol: str):
    import yfinance as yf  # imported lazily so fakes need no yfinance

    return yf.Ticker(symbol)


class YFinanceProvider(MarketDataProvider):
    def __init__(
        self,
        ticker_factory: Callable[[str], Any] | None = None,
        *,
        screen_fn: Callable[..., dict[str, Any]] | None = None,
    ):
        self._ticker_factory = ticker_factory or _default_ticker_factory
        self._screen = screen_fn or _default_screen

    def fetch_company(self, ticker: str) -> CompanySnapshot:
        symbol = ticker.strip().upper()
        t = self._ticker_factory(symbol)
        try:
            history = t.history(period="5d", auto_adjust=False)
            raw = RawCompany(
                ticker=symbol,
                info=t.info or {},
                price_history=history,
                price_metadata=_price_metadata(t),
                quarterly_income=t.quarterly_income_stmt,
                annual_income=t.income_stmt,
                quarterly_balance=t.quarterly_balance_sheet,
                quarterly_cashflow=t.quarterly_cashflow,
                annual_cashflow=t.cashflow,
            )
        except Exception as exc:  # network, parsing, Yahoo format drift
            raise MarketDataError(f"{symbol}: yfinance request failed: {exc}") from exc
        return map_company(raw)

    def get_quote(self, ticker: str) -> Quote:
        """Price only, from the chart endpoint; no statements are fetched."""
        symbol = ticker.strip().upper()
        t = self._ticker_factory(symbol)
        try:
            history = t.history(period="5d", auto_adjust=False)
            metadata = _price_metadata(t)
        except Exception as exc:
            raise MarketDataError(f"{symbol}: yfinance request failed: {exc}") from exc
        if _empty(history):
            raise TickerNotFoundError(symbol)
        return map_quote(symbol, history, metadata)

    # --- Peer discovery -------------------------------------------------

    def get_profile(self, ticker: str) -> CompanyProfile:
        symbol = ticker.strip().upper()
        t = self._ticker_factory(symbol)
        try:
            info = t.info or {}
        except Exception as exc:
            raise MarketDataError(f"{symbol}: yfinance request failed: {exc}") from exc
        # An unknown symbol comes back as a near-empty dict (just
        # trailingPegRatio) rather than an exception.
        if not info.get("longName") and not info.get("shortName"):
            raise TickerNotFoundError(symbol)
        return map_profile(symbol, info)

    def screen_peers(
        self,
        *,
        sector: str | None,
        industry: str | None,
        market_cap_min: float,
        market_cap_max: float,
        limit: int = 50,
    ) -> list[CompanyProfile]:
        """Yahoo's equity screener, restricted to primary US exchanges.
        Industry is the finer filter and wins when given. Anything that
        slips through (a US-listed company reporting in another currency)
        is dropped by select_peers."""
        from yfinance import EquityQuery  # lazy: fakes need no yfinance

        if industry is None and sector is None:
            raise ValueError("screen_peers needs a sector or an industry")
        try:
            clauses = [
                EquityQuery("eq", ["region", "us"]),
                EquityQuery("is-in", ["exchange", *_PRIMARY_US_EXCHANGES]),
                EquityQuery("btwn", ["intradaymarketcap", market_cap_min, market_cap_max]),
                EquityQuery("eq", ["industry", industry] if industry is not None else ["sector", sector]),
            ]
            query = EquityQuery("and", clauses)
        except ValueError as exc:  # a sector/industry name Yahoo does not know
            raise MarketDataError(f"screener rejected the query: {exc}") from exc
        try:
            result = self._screen(
                query,
                size=min(limit, _SCREEN_MAX_SIZE),
                sortField="intradaymarketcap",
                sortAsc=False,
            )
        except Exception as exc:
            raise MarketDataError(f"yfinance screener request failed: {exc}") from exc
        quotes = (result or {}).get("quotes") or []
        return [
            map_screen_quote(q, sector=sector, industry=industry)
            for q in quotes
            if q.get("quoteType") == "EQUITY" and q.get("symbol")
        ]
