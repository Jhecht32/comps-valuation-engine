# Comps Valuation Engine

## What it does

Comparable-companies analysis over public data, as an API. Given a target and a peer list, it pulls prices and statements from Yahoo Finance, builds each company's enterprise value and LTM multiples (EV/Revenue, EV/EBITDA, EV/EBIT, P/E), summarises the peer distribution (quartiles, median, mean, n after NM exclusion), and applies them back to the target for an implied per-share range. Every approximation travels with the result as a data-quality flag; a peer that cannot be valued lands in a per-ticker errors map rather than failing the run. FastAPI, yfinance, in-memory TTL cache.

## EV bridge

```
Equity value     = diluted shares × share price
Enterprise value = equity value + total debt + preferred equity + noncontrolling interest
                   − cash and equivalents − short-term investments
```

Diluted shares are required; basic shares are a flagged fallback. Missing debt or cash counts as zero, flagged. The implied range runs the bridge in reverse; a company's own multiple recovers its own share price exactly.

## LTM methodology

LTM = the four most recent reported quarters; FY + current YTD stub − prior-year stub is also supported. EBITDA = LTM EBIT + LTM D&A; EBIT is operating income, D&A comes from the cash flow statement. Diluted EPS = (LTM net income − preferred dividends − income to noncontrolling interests) ÷ current diluted shares. A multiple is NM when its denominator is zero or negative, EV is negative, or EV/EBITDA exceeds 100x; NM values are excluded from statistics. Percentiles use Excel's PERCENTILE.INC interpolation. Non-consecutive quarters, filings older than 135 days, and missing line items are flagged.

## Validation

The engine was tied line-by-line to a model built by hand from Home Depot's 10-Q (quarter ended 2026-08-02) and 10-K (FY2025), reproducing enterprise value of $370,576.8mm and EV/EBITDA of 14.628x exactly, pinned as `backend/tests/test_home_depot_golden.py`. A live test (`COMPS_LIVE_TESTS=1`) diffs Yahoo's current figures against it.

## Known limitations

- No adjustment for non-recurring items.
- LTM only, no forward estimates.
- No calendarization for non-December fiscal year ends.
- Yahoo's headline Total Debt bundles capitalized operating leases (62,567 vs. 52,896 for HD), so debt is built from component rows to stay consistent with unadjusted EBITDA.
- Filers that split tangible and intangible D&A require summing both cash-flow lines; the income-statement figure understates HD's EBITDA by roughly $850mm annually.
- Peer screening is mechanical, not judgment-based.
- Not investment advice.

## Setup

Requires Python 3.11+ and GNU Make.

```
make dev        # create backend/.venv, install, serve http://127.0.0.1:8000
make test       # offline suite
make test-live  # also hits Yahoo Finance
```

Docs at `/docs`. Endpoints: `GET /api/company/{ticker}`, `POST /api/comps`, `GET /api/peers/{ticker}`. Override `HOST`/`PORT` on the make command line.
