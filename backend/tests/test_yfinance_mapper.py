"""Unit tests for the yfinance mapper, against hand-built frames shaped
exactly like yfinance's: row names as index, period-end Timestamps as
columns (newest first), raw currency units, NaN for missing cells.

Values are Home Depot's as returned by yfinance on 2026-09-06 so the
end-to-end fake-ticker test ties to the golden model; the rule tests
override individual rows.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.data import MarketDataError, TickerNotFoundError
from app.data.yfinance_provider import RawCompany, YFinanceProvider, map_company, map_quote
from tests.helpers import value_snapshot

M = 1_000_000.0

Q_COLS = ["2026-07-31", "2026-04-30", "2026-01-31", "2025-10-31"]
A_COLS = ["2026-01-31", "2025-01-31"]
B_COLS = ["2026-07-31", "2026-04-30"]


def frame(cols, rows):
    """rows: name -> per-column values in millions (None -> NaN)."""
    idx = pd.DatetimeIndex([pd.Timestamp(c) for c in cols])
    data = {name: [np.nan if v is None else v * M for v in vals] for name, vals in rows.items()}
    return pd.DataFrame.from_dict(data, orient="index", columns=idx)


def history(closes):
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in closes])
    return pd.DataFrame({"Close": list(closes.values())}, index=idx)


HD_Q_INCOME = {
    "Total Revenue": [47_861, 41_765, 38_198, 41_352],
    "Gross Profit": [16_115, 13_781, 12_466, 13_815],
    "Operating Income": [6_839, 4_981, 3_849, 5_353],
    "EBIT": [6_898, 4_988, 3_892, 5_385],
    "Net Income": [4_766, 3_289, 2_571, 3_601],
    "Net Income Including Noncontrolling Interests": [4_766, 3_289, 2_571, 3_601],
    "Net Income Common Stockholders": [4_766, 3_289, 2_571, 3_601],
    "Diluted Average Shares": [996, 996, 995, 995],
    "Basic Average Shares": [994, 994, 993, 993],
}
HD_Q_CASHFLOW = {
    "Depreciation And Amortization": [1_107, 1_081, 1_079, 1_044],
    "Depreciation": [929, 910, 908, 886],
    "Amortization Of Intangibles": [178, 171, 171, 158],
}
HD_Q_BALANCE = {
    "Total Debt": [62_567, 63_157],
    "Capital Lease Obligations": [9_671, 9_648],
    "Long Term Debt": [43_951, 44_828],
    "Current Debt": [8_945, 8_681],
    "Cash And Cash Equivalents": [2_085, 1_601],
    "Stockholders Equity": [16_617, 13_874],
    "Common Stock Equity": [16_617, 13_874],
    "Total Equity Gross Minority Interest": [16_617, 13_874],
    "Ordinary Shares Number": [998, 997],
}
HD_A_INCOME = {
    "Total Revenue": [164_683, 159_514],
    "Gross Profit": [54_865, 53_308],
    "Operating Income": [20_890, 21_526],
    "Net Income": [14_156, 14_806],
    "Net Income Including Noncontrolling Interests": [14_156, 14_806],
    "Net Income Common Stockholders": [14_156, 14_806],
    "Diluted Average Shares": [995, 993],
}
HD_A_CASHFLOW = {
    "Depreciation And Amortization": [4_121, 3_761],
    "Depreciation": [3_514, 3_336],
    "Amortization Of Intangibles": [607, 425],
}
HD_INFO = {
    "longName": "The Home Depot, Inc.",
    "shortName": "Home Depot, Inc. (The)",
    "currency": "USD",
    "financialCurrency": "USD",
    "sharesOutstanding": 997_689_626,
}
# Yahoo's chart endpoint returns closes at float32 precision; the chart
# metadata carries the exact quoted price and its timestamp.
HD_HISTORY = {"2026-09-03": 317.89, "2026-09-04": 321.04998779296875}
HD_METADATA = {
    "currency": "USD",
    "regularMarketPrice": 321.05,
    "regularMarketTime": pd.Timestamp("2026-09-04 16:00:03", tz="America/New_York"),
}


def raw(**overrides) -> RawCompany:
    base = dict(
        ticker="HD",
        info=dict(HD_INFO),
        price_history=history(HD_HISTORY),
        price_metadata=dict(HD_METADATA),
        quarterly_income=frame(Q_COLS, HD_Q_INCOME),
        annual_income=frame(A_COLS, HD_A_INCOME),
        quarterly_balance=frame(B_COLS, HD_Q_BALANCE),
        quarterly_cashflow=frame(Q_COLS, HD_Q_CASHFLOW),
        annual_cashflow=frame(A_COLS, HD_A_CASHFLOW),
    )
    base.update(overrides)
    return RawCompany(**base)


def with_rows(base_rows, cols, **changes):
    """Copy of a row dict with rows replaced (value) or removed (None)."""
    rows = {k: list(v) for k, v in base_rows.items()}
    for name, vals in changes.items():
        key = name.replace("__", " ")
        if vals is None:
            rows.pop(key, None)
        else:
            rows[key] = vals
    return frame(cols, rows)


# --- Identity, price, currencies ------------------------------------------


def test_identity_price_and_currencies():
    s = map_company(raw())
    assert s.ticker == "HD"
    assert s.name == "The Home Depot, Inc."
    assert s.source == "yfinance"
    assert s.share_price == 321.05
    assert s.price_as_of == date(2026, 9, 4)
    assert s.price_currency == "USD"
    assert s.reporting_currency == "USD"


def test_adr_currencies_are_carried_not_collapsed():
    s = map_company(raw(info={**HD_INFO, "financialCurrency": "TWD"}))
    assert s.price_currency == "USD"
    assert s.reporting_currency == "TWD"


def test_name_falls_back_to_short_name():
    info = {k: v for k, v in HD_INFO.items() if k != "longName"}
    assert map_company(raw(info=info)).name == "Home Depot, Inc. (The)"


def test_empty_history_and_statements_is_ticker_not_found():
    empty = pd.DataFrame()
    with pytest.raises(TickerNotFoundError):
        map_company(
            raw(
                info={},
                price_history=empty,
                price_metadata={},
                quarterly_income=empty,
                annual_income=empty,
                quarterly_balance=empty,
                quarterly_cashflow=empty,
                annual_cashflow=empty,
            )
        )


def test_empty_history_with_statements_is_a_market_data_error():
    with pytest.raises(MarketDataError, match="price"):
        map_company(raw(price_history=pd.DataFrame(), price_metadata={"currency": "USD"}))


def test_map_quote_prefers_the_exact_quoted_price_in_chart_metadata():
    q = map_quote("HD", history(HD_HISTORY), HD_METADATA)
    assert q.share_price == 321.05
    assert q.price_as_of == date(2026, 9, 4)
    assert q.price_currency == "USD"


def test_map_quote_falls_back_to_the_last_close_bar():
    q = map_quote("HD", history({"2026-09-03": 317.89, "2026-09-04": 320.5}), {"currency": "USD"})
    assert q.share_price == 320.5
    assert q.price_as_of == date(2026, 9, 4)


def test_map_quote_ignores_metadata_price_without_a_timestamp():
    # A price with no date is not a quote the engine can label.
    meta = {"currency": "USD", "regularMarketPrice": 321.05}
    q = map_quote("HD", history({"2026-09-04": 320.5}), meta)
    assert q.share_price == 320.5


# --- Quarters and fiscal years ---------------------------------------------


def test_quarters_map_newest_first_in_raw_dollars():
    s = map_company(raw())
    assert [q.period_end for q in s.quarters] == [
        date(2026, 7, 31),
        date(2026, 4, 30),
        date(2026, 1, 31),
        date(2025, 10, 31),
    ]
    q = s.quarters[0]
    assert q.revenue == 47_861 * M
    assert q.gross_profit == 16_115 * M
    assert q.ebit == 6_839 * M
    assert q.net_income == 4_766 * M
    assert q.d_and_a == 1_107 * M


def test_fiscal_years_map_like_quarters():
    s = map_company(raw())
    assert [fy.period_end for fy in s.fiscal_years] == [date(2026, 1, 31), date(2025, 1, 31)]
    fy = s.fiscal_years[0]
    assert fy.revenue == 164_683 * M
    assert fy.ebit == 20_890 * M
    assert fy.net_income == 14_156 * M
    assert fy.d_and_a == 4_121 * M


def test_ebit_is_operating_income_not_yahoo_ebit():
    # Yahoo's "EBIT" row is pretax income plus interest, which folds in
    # non-operating items. Operating Income is what the golden model uses.
    s = map_company(raw())
    assert s.quarters[0].ebit == 6_839 * M != 6_898 * M


def test_nan_cell_is_none_and_zero_cell_is_zero():
    inc = with_rows(HD_Q_INCOME, Q_COLS, Gross__Profit=[None, 0, 12_466, 13_815])
    s = map_company(raw(quarterly_income=inc))
    assert s.quarters[0].gross_profit is None
    assert s.quarters[1].gross_profit == 0.0


def test_all_nan_period_is_dropped():
    cols = ["2026-10-31"] + Q_COLS
    rows = {k: [None] + v for k, v in HD_Q_INCOME.items()}
    s = map_company(raw(quarterly_income=frame(cols, rows)))
    assert s.quarters[0].period_end == date(2026, 7, 31)
    assert len(s.quarters) == 4


# --- D&A from the cash flow statement --------------------------------------


def test_da_prefers_the_cashflow_total_line():
    cf = with_rows(HD_Q_CASHFLOW, Q_COLS, Depreciation=[1, 1, 1, 1])  # would not sum to the total
    s = map_company(raw(quarterly_cashflow=cf))
    assert s.quarters[0].d_and_a == 1_107 * M


def test_da_sums_split_lines_when_no_total():
    cf = with_rows(
        HD_Q_CASHFLOW,
        Q_COLS,
        Depreciation__And__Amortization=None,
        Depreciation__Amortization__Depletion=None,
    )
    s = map_company(raw(quarterly_cashflow=cf))
    assert s.quarters[0].d_and_a == (929 + 178) * M


def test_da_uses_lone_depreciation_line_when_nothing_else_is_split_out():
    cf = frame(Q_COLS, {"Depreciation": [929, 910, 908, 886]})
    s = map_company(raw(quarterly_cashflow=cf))
    assert s.quarters[0].d_and_a == 929 * M


def test_da_is_none_when_the_cashflow_lacks_the_period():
    cf = frame(Q_COLS[1:], {k: v[1:] for k, v in HD_Q_CASHFLOW.items()})
    s = map_company(raw(quarterly_cashflow=cf))
    assert s.quarters[0].d_and_a is None
    assert s.quarters[1].d_and_a == 1_081 * M


# --- Income-to-common breakouts -------------------------------------------

WMT_LIKE = {
    "Total Revenue": [100_000] * 4,
    "Operating Income": [7_000] * 4,
    "Net Income": [6_366, 5_330, 4_237, 6_143],
    "Net Income Including Noncontrolling Interests": [6_529, 5_490, 4_392, 6_088],
    "Net Income Common Stockholders": [6_366, 5_330, 4_237, 6_143],
    "Minority Interests": [-163, -160, -155, 55],
    "Diluted Average Shares": [7_978] * 4,
}


def test_net_income_is_consolidated_including_nci():
    s = map_company(raw(quarterly_income=frame(Q_COLS, WMT_LIKE)))
    assert s.quarters[0].net_income == 6_529 * M


def test_nci_income_comes_from_the_income_statement_not_the_balance_sheet():
    # Income statement "Minority Interests" (a flow, -163 for the quarter)
    # vs balance sheet "Minority Interest" (a stock, 6,561). Crossing them
    # would put a balance in an income deduction.
    bs = with_rows(HD_Q_BALANCE, B_COLS, Minority__Interest=[6_561, 6_645])
    s = map_company(raw(quarterly_income=frame(Q_COLS, WMT_LIKE), quarterly_balance=bs))
    assert s.quarters[0].nci_income == 163 * M
    assert s.balance_sheet.noncontrolling_interest == 6_561 * M


def test_nci_income_sign_follows_yahoo_deduction_convention():
    # Yahoo shows the deduction as negative; a positive value is an NCI
    # loss that adds back to income to common.
    s = map_company(raw(quarterly_income=frame(Q_COLS, WMT_LIKE)))
    assert s.quarters[3].nci_income == -55 * M


def test_nci_income_known_zero_when_no_minority_line_and_ni_rows_agree():
    s = map_company(raw())
    assert all(q.nci_income == 0.0 for q in s.quarters)
    assert all(fy.nci_income == 0.0 for fy in s.fiscal_years)


def test_nci_income_derived_from_ni_rows_when_no_minority_line():
    rows = {k: v for k, v in WMT_LIKE.items() if k != "Minority Interests"}
    s = map_company(raw(quarterly_income=frame(Q_COLS, rows)))
    assert s.quarters[0].nci_income == (6_529 - 6_366) * M


def test_nci_income_none_when_not_derivable():
    rows = {
        k: v
        for k, v in WMT_LIKE.items()
        if k not in ("Minority Interests", "Net Income Including Noncontrolling Interests")
    }
    s = map_company(raw(quarterly_income=frame(Q_COLS, rows)))
    assert s.quarters[0].nci_income is None
    assert s.quarters[0].net_income == 6_366 * M  # falls back to Net Income


PSA_LIKE = {
    "Total Revenue": [1_200] * 4,
    "Operating Income": [600] * 4,
    "Net Income": [500.0, 526.3, 507.1, 511.1],
    "Net Income Including Noncontrolling Interests": [502.9, 529.4, 510.1, 514.8],
    "Net Income Common Stockholders": [450.3, 476.8, 457.0, 461.4],
    "Preferred Stock Dividends": [48.7] * 4,
    "Minority Interests": [-3.0, -3.1, -3.0, -3.7],
    "Diluted Average Shares": [176.5] * 4,
}


def test_preferred_dividends_from_the_direct_line():
    s = map_company(raw(quarterly_income=frame(Q_COLS, PSA_LIKE)))
    assert s.quarters[0].preferred_dividends == pytest.approx(48.7 * M)


def test_preferred_dividends_known_zero_when_ni_equals_ni_to_common():
    s = map_company(raw())
    assert all(q.preferred_dividends == 0.0 for q in s.quarters)


def test_preferred_dividends_derived_from_ni_rows_when_no_direct_line():
    rows = {k: v for k, v in PSA_LIKE.items() if k != "Preferred Stock Dividends"}
    s = map_company(raw(quarterly_income=frame(Q_COLS, rows)))
    assert s.quarters[0].preferred_dividends == pytest.approx((500.0 - 450.3) * M)


def test_preferred_dividends_none_when_not_derivable():
    rows = {
        k: v
        for k, v in PSA_LIKE.items()
        if k not in ("Preferred Stock Dividends", "Net Income Common Stockholders")
    }
    s = map_company(raw(quarterly_income=frame(Q_COLS, rows)))
    assert s.quarters[0].preferred_dividends is None


# --- Balance sheet ---------------------------------------------------------


def test_balance_sheet_uses_the_most_recent_column():
    s = map_company(raw())
    assert s.balance_sheet.as_of == date(2026, 7, 31)
    assert s.balance_sheet.cash_and_equivalents == 2_085 * M


def test_total_debt_excludes_capital_lease_obligations():
    # Yahoo's Total Debt (62,567) folds in 9,671 of operating leases under
    # "Capital Lease Obligations". Long Term Debt + Current Debt is the
    # borrowed money the golden model uses.
    s = map_company(raw())
    assert s.balance_sheet.total_debt == (43_951 + 8_945) * M


def test_total_debt_falls_back_to_total_debt_less_leases():
    bs = with_rows(HD_Q_BALANCE, B_COLS, Long__Term__Debt=None, Current__Debt=None)
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.total_debt == (62_567 - 9_671) * M


def test_total_debt_uses_whichever_debt_line_is_present():
    bs = with_rows(HD_Q_BALANCE, B_COLS, Current__Debt=None)
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.total_debt == 43_951 * M


def test_total_debt_none_when_no_debt_rows():
    bs = with_rows(
        HD_Q_BALANCE, B_COLS, Long__Term__Debt=None, Current__Debt=None, Total__Debt=None
    )
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.total_debt is None


def test_short_term_investments_absent_is_none():
    s = map_company(raw())
    assert s.balance_sheet.short_term_investments is None


def test_short_term_investments_present_is_mapped():
    bs = with_rows(HD_Q_BALANCE, B_COLS, Other__Short__Term__Investments=[500, 400])
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.short_term_investments == 500 * M


def test_preferred_and_nci_known_zero_from_equity_totals():
    # No preferred or minority rows, but Yahoo reports common equity,
    # stockholders' equity and total equity gross of NCI all equal: the
    # source is stating both are zero, not omitting them.
    s = map_company(raw())
    assert s.balance_sheet.preferred_equity == 0.0
    assert s.balance_sheet.noncontrolling_interest == 0.0


def test_preferred_and_nci_from_direct_rows():
    bs = with_rows(
        HD_Q_BALANCE,
        B_COLS,
        Preferred__Stock__Equity=[4_350, 4_350],
        Minority__Interest=[95.5, 95.4],
    )
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.preferred_equity == 4_350 * M
    assert s.balance_sheet.noncontrolling_interest == pytest.approx(95.5 * M)


def test_preferred_and_nci_derived_from_equity_totals_when_rows_absent():
    bs = with_rows(
        HD_Q_BALANCE,
        B_COLS,
        Common__Stock__Equity=[12_000, 12_000],
        Stockholders__Equity=[16_350, 16_350],
        Total__Equity__Gross__Minority__Interest=[16_500, 16_500],
    )
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.preferred_equity == 4_350 * M
    assert s.balance_sheet.noncontrolling_interest == 150 * M


def test_preferred_and_nci_none_when_equity_totals_are_missing():
    bs = with_rows(
        HD_Q_BALANCE,
        B_COLS,
        Common__Stock__Equity=None,
        Total__Equity__Gross__Minority__Interest=None,
    )
    s = map_company(raw(quarterly_balance=bs))
    assert s.balance_sheet.preferred_equity is None
    assert s.balance_sheet.noncontrolling_interest is None


# --- Shares ----------------------------------------------------------------


def test_shares_come_from_the_latest_quarter():
    s = map_company(raw())
    assert s.diluted_shares == 996 * M
    assert s.basic_shares == 994 * M
    assert s.shares_as_of == date(2026, 7, 31)


def test_diluted_shares_none_when_not_reported():
    inc = with_rows(HD_Q_INCOME, Q_COLS, Diluted__Average__Shares=None)
    s = map_company(raw(quarterly_income=inc))
    assert s.diluted_shares is None
    assert s.basic_shares == 994 * M


def test_basic_shares_fall_back_to_info_shares_outstanding():
    inc = with_rows(HD_Q_INCOME, Q_COLS, Basic__Average__Shares=None)
    s = map_company(raw(quarterly_income=inc))
    assert s.basic_shares == 997_689_626


# --- End to end through the engine ----------------------------------------


def test_hd_like_frames_reproduce_the_golden_multiples():
    s = map_company(raw())
    ltm, bridge, eps, multiples = value_snapshot(s, today=date(2026, 9, 4))
    assert ltm.revenue == 169_176 * M
    assert ltm.ebit == 21_022 * M
    assert ltm.net_income == 14_227 * M
    assert ltm.ebitda == 25_333 * M
    assert ltm.flags == []
    assert bridge.enterprise_value == pytest.approx(370_576.8 * M)
    assert bridge.preferred_equity_reported is True
    assert bridge.noncontrolling_interest_reported is True
    assert bridge.short_term_investments_reported is False
    assert eps.flags == []
    assert round(multiples.ev_ebitda.value, 3) == 14.628


# --- Provider wiring with a fake yfinance Ticker ---------------------------


class FakeTicker:
    def __init__(self, r: RawCompany, *, explode_on=None):
        self._r = r
        self.explode_on = explode_on
        self.touched: list[str] = []

    def _get(self, name, value):
        self.touched.append(name)
        if self.explode_on == name:
            raise ConnectionError("yahoo is down")
        return value

    @property
    def info(self):
        return self._get("info", self._r.info)

    def history(self, **kwargs):
        return self._get("history", self._r.price_history)

    @property
    def history_metadata(self):
        return self._r.price_metadata

    @property
    def quarterly_income_stmt(self):
        return self._get("quarterly_income_stmt", self._r.quarterly_income)

    @property
    def income_stmt(self):
        return self._get("income_stmt", self._r.annual_income)

    @property
    def quarterly_balance_sheet(self):
        return self._get("quarterly_balance_sheet", self._r.quarterly_balance)

    @property
    def quarterly_cashflow(self):
        return self._get("quarterly_cashflow", self._r.quarterly_cashflow)

    @property
    def cashflow(self):
        return self._get("cashflow", self._r.annual_cashflow)


def test_provider_maps_through_a_ticker_factory():
    fake = FakeTicker(raw())
    provider = YFinanceProvider(ticker_factory=lambda symbol: fake)
    s = provider.get_company("hd")
    assert s.ticker == "HD"
    assert s.share_price == 321.05
    assert s.balance_sheet.total_debt == 52_896 * M


def test_provider_quote_touches_only_price_history():
    fake = FakeTicker(raw())
    provider = YFinanceProvider(ticker_factory=lambda symbol: fake)
    q = provider.get_quote("HD")
    assert q.share_price == 321.05
    assert q.price_currency == "USD"
    assert fake.touched == ["history"]


def test_provider_wraps_source_failures_as_market_data_errors():
    fake = FakeTicker(raw(), explode_on="quarterly_income_stmt")
    provider = YFinanceProvider(ticker_factory=lambda symbol: fake)
    with pytest.raises(MarketDataError, match="yahoo is down"):
        provider.get_company("HD")
