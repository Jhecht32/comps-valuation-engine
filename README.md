# Comps Valuation Engine

## What it does

Comparable-companies analysis over public data, as an API. Given a target and peers, it pulls prices and statements from Yahoo Finance, builds each company's enterprise value and LTM multiples (EV/Revenue, EV/EBITDA, EV/EBIT, P/E), summarises the peer distribution, and applies them back to the target for an implied per-share range. Every approximation travels with the result as a data-quality flag; an unvaluable peer lands in a per-ticker errors map rather than failing the run. FastAPI, yfinance, TTL cache, single-file HTML frontend.

![Comps UI: headline comparison, comps table with the target row shaded, peer statistics, and implied per-share range](docs/screenshots/comps-ui.png)

## EV bridge

```
Equity value     = diluted shares × share price
Enterprise value = equity value + total debt + preferred equity + noncontrolling interest
                   − cash and equivalents − short-term investments
```

Diluted shares are required; basic shares are a flagged fallback. Missing debt or cash counts as zero, flagged. The implied range runs the bridge in reverse; a company's own multiple recovers its own share price exactly.

## LTM methodology

LTM = the four most recent reported quarters; FY + current YTD stub − prior-year stub is also supported. EBITDA = LTM EBIT + LTM D&A; EBIT is operating income, D&A from the cash flow statement. Diluted EPS = (LTM net income − preferred dividends − income to noncontrolling interests) ÷ current diluted shares. A multiple is NM when its denominator is zero or negative, EV is negative, or EV/EBITDA exceeds 100x; NM values are excluded from statistics. Percentiles follow Excel's PERCENTILE.INC. Non-consecutive quarters, filings older than 135 days, and missing line items are flagged.

## UI

A single HTML file served at `/` by the same FastAPI app. Enter a target ticker and press Run: the page fetches a suggested peer set from `/api/peers`, fills the editable peers field, and posts the run to `/api/comps`. Edit the list and use "Run with listed peers" to re-value against your own set; "Reset to suggested" puts the screen's list back.

The result opens with a one-line headline placing the target's EV/EBITDA against the peer median as a premium or discount. Below it, the comps table shows each company's share price, diluted shares, EV bridge, LTM revenue and EBITDA, margin, and the four multiples; the target row is shaded, and any target multiple outside the peer interquartile range is highlighted with the 25th or 75th percentile in its tooltip. Every column header carries a definition.

Peer statistics (min, quartiles, median, mean, max, with NM excluded and n shown per multiple) sit beside the implied per-share range, where low, mid, and high are the peer 25th percentile, median, and 75th percentile applied through the EV bridge. A data-quality section lists every flag by ticker and every peer that could not be valued, with the reason. A collapsible "How to read this" panel explains equity vs. enterprise value, numerator-denominator pairing, LTM, NM, and how to read the range.

## Validation

The engine was tied line-by-line to a model built by hand from Home Depot's 10-Q (quarter ended 2026-08-02) and 10-K (FY2025), reproducing enterprise value of $370,576.8mm and EV/EBITDA of 14.628x exactly, pinned as `backend/tests/test_home_depot_golden.py`. A live test (`COMPS_LIVE_TESTS=1`) diffs Yahoo's current figures against it.

## Known limitations

- No adjustment for non-recurring items.
- LTM only, no forward estimates.
- No calendarization for non-December fiscal year ends.
- Yahoo's headline Total Debt bundles capitalized operating leases (62,567 vs. 52,896 for HD), so debt is built from component rows to stay consistent with unadjusted EBITDA.
- Filers that split tangible and intangible D&A require summing both cash-flow lines; the income-statement figure understates HD's EBITDA by roughly $850mm annually.
- Peer screening is mechanical (same industry, market cap 0.33x–3.0x, widened to sector and flagged when thin). For HD, the industry screen returns a single company, and widening to the sector fills the set with unrelated consumer names. The screen is a starting point that expects manual curation: prune and add names before trusting the output.
- Not investment advice.

## Setup

Requires Python 3.11+ and GNU Make.

```
make dev        # create backend/.venv, install, serve UI + API at http://127.0.0.1:8000
make test       # offline suite
make test-live  # also hits Yahoo Finance
```

UI at `/`, docs at `/docs`. Endpoints: `GET /api/company/{ticker}`, `POST /api/comps`, `GET /api/peers/{ticker}`.
