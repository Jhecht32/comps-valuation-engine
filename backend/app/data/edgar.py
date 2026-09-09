"""EDGAR proxy peer source: fetch a company's latest DEF 14A and parse
its compensation peer group.

Three requests per cold ticker: the SEC's company_tickers.json (ticker
to CIK and the name index, cached for a day), the submissions feed for
the CIK (which lists filings by form), and the proxy's primary
document. Results are cached per ticker for a day; failures are not.

The SEC's fair-access policy requires a User-Agent that names the
caller and a contact ("Name email@example.com") and rate-limits to ten
requests a second; the client declares one and spaces its requests.
Set COMPS_SEC_USER_AGENT to a real contact before running this against
EDGAR in earnest.
"""

import os
import threading
import time
from datetime import timedelta
from typing import Any, Callable

import requests

from app.data.proxy_peers import (
    EdgarUnavailableError,
    NameIndex,
    ProxyFilingNotFoundError,
    ProxyPeerGroup,
    ProxyPeerSource,
    document_url,
    extract_peer_group,
    html_to_lines,
    latest_filing,
    normalise_sec_ticker,
)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DEFAULT_USER_AGENT = "comps-valuation-engine admin@example.com"
USER_AGENT_ENV = "COMPS_SEC_USER_AGENT"


def user_agent_from_env() -> str:
    return os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT


class EdgarClient:
    """Thin HTTP client for sec.gov with the headers EDGAR requires and a
    minimum spacing between requests."""

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        timeout: float = 30.0,
        min_interval: float = 0.12,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._session = session or requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent or user_agent_from_env(), "Accept-Encoding": "gzip, deflate"}
        )
        self._timeout = timeout
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request = -1e9

    def _get(self, url: str, ticker: str) -> requests.Response:
        with self._lock:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            try:
                response = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as err:
                raise EdgarUnavailableError(ticker, f"EDGAR request failed: {err}") from err
            finally:
                self._last_request = self._clock()
        if response.status_code == 404:
            raise ProxyFilingNotFoundError(ticker, f"EDGAR has nothing at {url}")
        if response.status_code != 200:
            raise EdgarUnavailableError(ticker, f"EDGAR returned HTTP {response.status_code} for {url}")
        return response

    def get_json(self, url: str, *, ticker: str = "EDGAR") -> Any:
        try:
            return self._get(url, ticker).json()
        except ValueError as err:
            raise EdgarUnavailableError(ticker, f"EDGAR returned malformed JSON for {url}") from err

    def get_text(self, url: str, *, ticker: str = "EDGAR") -> str:
        return self._get(url, ticker).text


class EdgarProxyPeerSource(ProxyPeerSource):
    def __init__(
        self,
        client: EdgarClient | None = None,
        *,
        ttl: timedelta = timedelta(hours=24),
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client or EdgarClient()
        self._ttl = ttl.total_seconds()
        self._clock = clock
        self._index: tuple[float, NameIndex] | None = None
        self._groups: dict[str, tuple[float, ProxyPeerGroup]] = {}
        self._lock = threading.Lock()

    def name_index(self) -> NameIndex:
        with self._lock:
            if self._index is not None and self._clock() - self._index[0] <= self._ttl:
                return self._index[1]
        payload = self._client.get_json(COMPANY_TICKERS_URL)
        index = NameIndex.from_sec_company_tickers(payload)
        with self._lock:
            self._index = (self._clock(), index)
        return index

    def proxy_peer_group(self, ticker: str) -> ProxyPeerGroup:
        key = normalise_sec_ticker(ticker)
        with self._lock:
            cached = self._groups.get(key)
            if cached is not None and self._clock() - cached[0] <= self._ttl:
                return cached[1].model_copy(deep=True)

        index = self.name_index()
        entry = index.by_ticker(key)
        if entry is None:
            raise ProxyFilingNotFoundError(key, "ticker is not in the SEC's company list; no proxy statement to read")
        submissions = self._client.get_json(SUBMISSIONS_URL.format(cik=entry.cik), ticker=key)
        filing = latest_filing(submissions, form="DEF 14A")
        if filing is None:
            raise ProxyFilingNotFoundError(key, f"no DEF 14A on file for {entry.title}")
        url = document_url(entry.cik, filing.accession_number, filing.primary_document)
        html = self._client.get_text(url, ticker=key)
        extracted = extract_peer_group(html_to_lines(html), index, target_ticker=key)
        group = ProxyPeerGroup(
            **extracted.model_dump(),
            ticker=key,
            company_name=submissions.get("name") or entry.title,
            filing_date=filing.filing_date,
            accession_number=filing.accession_number,
            document_url=url,
        )
        with self._lock:
            self._groups[key] = (self._clock(), group)
        return group.model_copy(deep=True)
