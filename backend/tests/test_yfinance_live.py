"""Live integration test: pull Home Depot from Yahoo Finance and diff it
line by line against the golden hand model built from the filings.

Skipped unless COMPS_LIVE_TESTS=1 (network, and Yahoo's numbers move).
Run with -s to see the table even when it passes:

    COMPS_LIVE_TESTS=1 .venv/bin/pytest tests/test_yfinance_live.py -s

The golden values come straight from the 10-Q/10-K and are never
adjusted to match the source. If a line disagrees, either the mapper
adapts (a mapping rule, documented in yfinance_provider.py) or the gap
is recorded there as a known data-source limitation. The acceptance
criterion is EV/EBITDA within half a turn of the golden 14.628x; the
table exists so a miss names the field, not just the multiple.
"""

import os
from datetime import date

import pytest

from app.data.peers import MARKET_CAP_HIGH_MULTIPLE, MARKET_CAP_LOW_MULTIPLE

from app.data import CurrencyMismatchError, TickerNotFoundError
from app.data.yfinance_provider import YFinanceProvider
from tests.helpers import value_snapshot

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("COMPS_LIVE_TESTS"), reason="set COMPS_LIVE_TESTS=1 to hit Yahoo"),
]

M = 1_000_000

# Golden model, from tests/test_home_depot_golden.py ($mm, shares mm)
GOLDEN = {
    "share price ($)": 321.05,
    "diluted shares (mm)": 996.0,
    "total debt": 52_896.0,
    "cash": 2_085.0,
    "LTM revenue": 169_176.0,
    "LTM EBIT": 21_022.0,
    "LTM net income": 14_227.0,
    "LTM D&A": 4_311.0,
    "LTM EBITDA": 25_333.0,
    "equity value": 319_765.8,
    "enterprise value": 370_576.8,
    "EV/EBITDA (x)": 14.628,
}
GOLDEN_LTM_AS_OF = date(2026, 8, 2)
EV_EBITDA_TOLERANCE = 0.5


def _mm(value):
    return None if value is None else value / M


def _row(label, golden, actual):
    if actual is None:
        return f"  {label:<24} {golden:>14,.3f} {'None':>14} {'':>12}"
    delta = actual - golden
    pct = f"{delta / golden:+.2%}" if golden else ""
    return f"  {label:<24} {golden:>14,.3f} {actual:>14,.3f} {delta:>+12,.3f} {pct:>9}"


def test_hd_live_diff_against_golden_model():
    import yfinance as yf

    ticker = yf.Ticker("HD")
    provider = YFinanceProvider(ticker_factory=lambda symbol: ticker)
    snapshot = provider.get_company("HD")
    ltm, bridge, eps, multiples = value_snapshot(snapshot, today=snapshot.price_as_of)

    # Raw rows straight off Yahoo's balance sheet, before any mapping.
    bs = ticker.quarterly_balance_sheet
    latest = max(bs.columns)

    def raw(row):
        return _mm(float(bs.at[row, latest])) if row in bs.index else None

    actual = {
        "share price ($)": snapshot.share_price,
        "diluted shares (mm)": _mm(snapshot.diluted_shares),
        "total debt": _mm(bridge.total_debt),
        "cash": _mm(bridge.cash_and_equivalents),
        "LTM revenue": _mm(ltm.revenue),
        "LTM EBIT": _mm(ltm.ebit),
        "LTM net income": _mm(ltm.net_income),
        "LTM D&A": _mm(ltm.ebitda - ltm.ebit) if ltm.ebitda is not None else None,
        "LTM EBITDA": _mm(ltm.ebitda),
        "equity value": _mm(bridge.equity_value),
        "enterprise value": _mm(bridge.enterprise_value),
        "EV/EBITDA (x)": multiples.ev_ebitda.value,
    }

    lines = [
        "",
        f"HD: golden (filings, LTM to {GOLDEN_LTM_AS_OF}, price 2026-09-04) vs yfinance "
        f"(LTM to {ltm.as_of}, price {snapshot.price_as_of}); $mm unless noted",
        f"  {'line':<24} {'golden':>14} {'yfinance':>14} {'delta':>12} {'':>9}",
    ]
    lines += [_row(k, GOLDEN[k], actual[k]) for k in GOLDEN]
    lines += [
        "",
        "  Raw Yahoo balance sheet rows (mapped total debt = Long Term Debt + Current Debt):",
        f"  {'Total Debt':<24} {raw('Total Debt')!s:>14}",
        f"  {'Capital Lease Obligations':<24} {raw('Capital Lease Obligations')!s:>14}",
        f"  {'Long Term Debt':<24} {raw('Long Term Debt')!s:>14}",
        f"  {'Current Debt':<24} {raw('Current Debt')!s:>14}",
        "",
        f"  quarters: {[q.period_end.isoformat() for q in snapshot.quarters]}",
        f"  LTM flags: {[f.code.value for f in ltm.flags]}   bridge flags: {[f.code.value for f in bridge.flags]}   "
        f"EPS flags: {[f.code.value for f in eps.flags]}",
        f"  reported: preferred={bridge.preferred_equity_reported} NCI={bridge.noncontrolling_interest_reported} "
        f"ST investments={bridge.short_term_investments_reported}",
    ]
    report = "\n".join(lines)
    print(report)

    assert not multiples.ev_ebitda.is_nm, report
    assert abs(multiples.ev_ebitda.value - GOLDEN["EV/EBITDA (x)"]) <= EV_EBITDA_TOLERANCE, report

    # Structural contracts that should hold for HD regardless of the date.
    assert bridge.preferred_equity_reported is True, report
    assert bridge.noncontrolling_interest_reported is True, report
    assert bridge.short_term_investments_reported is False, report
    assert eps.flags == [], report
    assert snapshot.price_currency == snapshot.reporting_currency == "USD"


def test_adr_is_rejected_by_the_currency_guard():
    with pytest.raises(CurrencyMismatchError) as excinfo:
        YFinanceProvider().get_company("TSM")
    assert excinfo.value.price_currency == "USD"
    assert excinfo.value.reporting_currency == "TWD"


def test_unknown_ticker_is_not_found():
    with pytest.raises(TickerNotFoundError):
        YFinanceProvider().get_company("NOPE1234XYZ")


# --- Peer discovery ---------------------------------------------------------


def test_live_profile_and_screen_find_lowes_for_home_depot():
    """Pins the two assumptions the peer endpoint rests on: Ticker.info
    classifies HD, and screener quotes carry financialCurrency (without
    it select_peers drops every candidate and the endpoint returns an
    empty list with no error)."""
    provider = YFinanceProvider()
    hd = provider.get_profile("HD")
    assert hd.sector == "Consumer Cyclical"
    assert hd.industry == "Home Improvement Retail"
    assert hd.market_cap and hd.market_cap > 100e9
    assert (hd.price_currency, hd.reporting_currency) == ("USD", "USD")

    found = provider.screen_peers(
        sector=hd.sector,
        industry=hd.industry,
        market_cap_min=hd.market_cap * MARKET_CAP_LOW_MULTIPLE,
        market_cap_max=hd.market_cap * MARKET_CAP_HIGH_MULTIPLE,
    )
    by_ticker = {p.ticker: p for p in found}
    assert "LOW" in by_ticker, sorted(by_ticker)
    assert all(p.reporting_currency is not None for p in found), [p.ticker for p in found if p.reporting_currency is None]
    assert all(p.price_currency == "USD" for p in found)
    assert by_ticker["LOW"].industry == "Home Improvement Retail"
