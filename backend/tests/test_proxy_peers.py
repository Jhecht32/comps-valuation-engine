"""Tests for proxy-statement peer groups: name normalisation and the SEC
index, filing selection, HTML-to-lines, the parser on recorded excerpts
of the Home Depot, Nike, and Lowe's 2026 proxies plus synthetic edge
cases, and the EDGAR source over a fake client (no network).

tests/fixtures/proxies/ holds the peer-group section of each DEF 14A as
filed (HD 2026-04-07, NKE 2026-07-15, LOW 2026-04-16) and a subset of
the SEC's company_tickers.json covering the names they list.
"""

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from app.data.edgar import (
    COMPANY_TICKERS_URL,
    DEFAULT_USER_AGENT,
    USER_AGENT_ENV,
    EdgarClient,
    EdgarProxyPeerSource,
)
from app.data.proxy_peers import (
    Confidence,
    EdgarUnavailableError,
    FixtureProxyPeerSource,
    NameIndex,
    PeerGroupNotFoundError,
    ProxyFilingNotFoundError,
    ProxyPeer,
    ProxyPeerGroup,
    confidence_for,
    document_url,
    extract_peer_group,
    html_to_lines,
    latest_filing,
    name_key,
)

FIXTURES = Path(__file__).parent / "fixtures" / "proxies"
PROSE = "In reviewing the benchmarking data the committee considered each executive's compensation history and market position."


@pytest.fixture(scope="module")
def tickers_payload():
    return json.loads((FIXTURES / "company_tickers_subset.json").read_text())


@pytest.fixture(scope="module")
def index(tickers_payload):
    return NameIndex.from_sec_company_tickers(tickers_payload)


def excerpt(ticker: str) -> list[str]:
    return html_to_lines((FIXTURES / f"{ticker}.html").read_text())


# --- name_key and the index ----------------------------------------------


@pytest.mark.parametrize(
    "proxy_name, sec_title",
    [
        ("The TJX Companies, Inc.", "TJX COMPANIES INC /DE/"),
        ("Costco Wholesale Corporation", "COSTCO WHOLESALE CORP /NEW"),
        ("O’Reilly Automotive, Inc.", "O REILLY AUTOMOTIVE INC"),
        ("Amazon.com", "AMAZON COM INC"),
        ("Lowe's Companies, Inc.", "LOWES COMPANIES INC"),
        ("The Coca-Cola Company", "COCA COLA CO"),
        ("Best Buy Company, Inc.", "BEST BUY CO INC"),
        ("Procter & Gamble Company", "PROCTER & GAMBLE Co"),
        ("Walmart Inc.(1)", "Walmart Inc."),
        ("Anheuser-Busch InBev SA/NV", "Anheuser-Busch InBev SA/NV"),
        ("Kimco Realty L.P.", "KIMCO REALTY LP"),
        ("Applied Materials", "APPLIED MATERIALS INC /DE"),
    ],
)
def test_name_key_reconciles_proxy_spelling_with_sec_title(proxy_name, sec_title):
    assert name_key(proxy_name) == name_key(sec_title)


def test_name_key_keeps_distinguishing_words():
    assert name_key("Capri Holdings Limited") == "capriholdings"
    assert name_key("Target Hospitality Corp.") != name_key("Target Corporation")
    assert name_key("The") == "" and name_key("") == ""  # nothing left after the article


def test_index_lookup_and_ticker_forms(index):
    assert index.lookup("Alphabet Inc.").ticker == "GOOGL"  # first row wins for a dual class
    assert index.lookup("The Home Depot, Inc.").cik == 354950
    assert index.by_ticker("brk.b").ticker == "BRK-B" and index.by_ticker("BRK-B").title == "BERKSHIRE HATHAWAY INC"
    assert index.lookup("Nobody Corp") is None and index.lookup("") is None
    assert len(index) == 36


# --- Filing selection -----------------------------------------------------


def submissions(rows, name="HOME DEPOT, INC."):
    return {
        "name": name,
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "accessionNumber": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "primaryDocument": [r[3] for r in rows],
            }
        },
    }


HD_SUBMISSIONS = submissions(
    [
        ("10-Q", "0000354950-26-000120", "2026-08-25", "hd-20260802.htm"),
        ("DEFA14A", "0000354950-26-000091", "2026-04-07", "hd-additional.htm"),
        ("DEF 14A", "0000354950-26-000090", "2026-04-07", "hd-20260406.htm"),
        ("DEF 14A", "0000354950-25-000123", "2025-04-07", "hd-20250407.htm"),
    ]
)


def test_latest_filing_picks_the_newest_of_the_exact_form():
    filing = latest_filing(HD_SUBMISSIONS)
    assert (filing.accession_number, filing.filing_date, filing.primary_document) == (
        "0000354950-26-000090", date(2026, 4, 7), "hd-20260406.htm",
    )
    assert latest_filing(HD_SUBMISSIONS, form="10-K") is None
    assert latest_filing({}) is None


def test_document_url_strips_accession_dashes():
    assert document_url(354950, "0000354950-26-000090", "hd-20260406.htm") == (
        "https://www.sec.gov/Archives/edgar/data/354950/000035495026000090/hd-20260406.htm"
    )


# --- HTML to lines --------------------------------------------------------


def test_html_to_lines_splits_cells_and_joins_inline_spans():
    html = (
        "<style>td{color:red}</style><script>x=1</script>"
        "<div><span>The companies in the Peer Group for fiscal 2025 </span><span>were</span>:</div>"
        "<table><tr><td>Best&#160;Buy Co., Inc.</td><td>$41,528</td></tr>"
        "<tr><td><b>Costco</b> Wholesale Corporation<br/></td><td></td></tr></table>"
    )
    assert html_to_lines(html) == [
        "The companies in the Peer Group for fiscal 2025 were:",
        "Best Buy Co., Inc.",
        "$41,528",
        "Costco Wholesale Corporation",
    ]


# --- Parser on recorded proxy excerpts ------------------------------------


def test_home_depot_retail_peer_group(index):
    group = extract_peer_group(excerpt("HD"), index, target_ticker="HD")
    assert [p.ticker for p in group.peers] == ["AMZN", "ROST", "AZO", "TGT", "COST", "KR", "LOW", "TJX", "ORLY", "WMT"]
    assert group.peers[0].name == "Amazon.com" and group.peers[6].name == "Lowe’s Companies, Inc."
    assert group.unmatched == []
    assert group.confidence is Confidence.HIGH
    assert group.context == "Retail Peer Group"


def test_nike_peer_group_is_a_talent_set_not_an_industry(index):
    group = extract_peer_group(excerpt("NKE"), index, target_ticker="NKE")
    tickers = [p.ticker for p in group.peers]
    assert len(tickers) == 17 and tickers[:3] == ["BBY", "MSFT", "SBUX"] and tickers[-1] == "CRM"
    assert group.unmatched == [] and group.confidence is Confidence.HIGH
    assert group.context.startswith("Given the competitive market for top-tier talent")


def test_lowes_peer_group_reports_a_delisted_name_as_unmatched(index):
    group = extract_peer_group(excerpt("LOW"), index, target_ticker="LOW")
    assert [p.ticker for p in group.peers] == ["BBY", "COST", "CVS", "DG", "NKE", "SBUX", "TGT", "HD", "KR", "TJX", "WMT"]
    assert group.unmatched == ["Walgreens Boots Alliance, Inc."]  # taken private in 2025
    assert group.confidence is Confidence.HIGH
    assert group.context == "The companies in the Peer Group for fiscal 2025 were:"


def test_the_filer_is_never_its_own_peer(index):
    group = extract_peer_group(excerpt("LOW"), index, target_ticker="hd")
    assert "HD" not in [p.ticker for p in group.peers] and len(group.peers) == 10


# --- Parser edge cases ----------------------------------------------------


def test_list_survives_page_breaks_and_table_columns(index):
    lines = [
        "Peer Group",
        "Target Corporation", "$107,412", "$65,000",
        "The Home Depot 2026 Proxy Statement", "59", "Table of Contents",
        "Walmart Inc.", "$681,000", "$700,000",
        "Costco Wholesale Corporation", "—", "n/a",
        PROSE,
        "The Kroger Co.",  # after the paragraph: a different list
    ]
    group = extract_peer_group(lines, index, target_ticker="HD")
    assert [p.ticker for p in group.peers] == ["TGT", "WMT", "COST"]
    assert group.unmatched == []


def test_unmatched_names_are_reported_not_dropped(index):
    lines = [
        "The peer group consisted of:",
        "Target Corporation",
        "adidas AG",
        "LVMH Moët Hennessy Louis Vuitton SE",
        "Walmart Inc.",
        "Puma SE",                    # trailing, carries a corporate word: kept
        "Compensation Consultant",    # trailing heading without one: not a name
        PROSE,
    ]
    group = extract_peer_group(lines, index, target_ticker="HD")
    assert [p.ticker for p in group.peers] == ["TGT", "WMT"]
    assert group.unmatched == ["adidas AG", "LVMH Moët Hennessy Louis Vuitton SE", "Puma SE"]
    assert group.confidence is Confidence.LOW


def test_interior_lines_without_a_corporate_word_count_as_unmatched(index):
    lines = ["Peer Group", "Target Corporation", "Ulta Beauty", "Walmart Inc."]
    group = extract_peer_group(lines, index, target_ticker="HD")
    assert group.unmatched == ["Ulta Beauty"]


def test_bare_target_is_compensation_vocabulary(index):
    with pytest.raises(PeerGroupNotFoundError):
        extract_peer_group(["Peer Group", "Threshold", "Target", "Maximum"], index, target_ticker="HD")
    group = extract_peer_group(["Peer Group", "Target Corporation", "Walmart Inc."], index, target_ticker="HD")
    assert [p.ticker for p in group.peers] == ["TGT", "WMT"]


def test_best_scoring_run_wins_and_nearest_anchor_names_the_context(index):
    lines = [
        "We do not target any specific peer group percentile ranking.",
        "Walmart Inc.",
        "Target Corporation",
        PROSE,
        "The retail peer group was unchanged from Fiscal 2024.",
        "Retail Peer Group",
        "Amazon.com", "Ross Stores, Inc.", "AutoZone, Inc.", "The Kroger Co.", "Lowe’s Companies, Inc.",
        PROSE,
    ]
    group = extract_peer_group(lines, index, target_ticker="HD")
    assert [p.ticker for p in group.peers] == ["AMZN", "ROST", "AZO", "KR", "LOW"]
    assert group.context == "Retail Peer Group"


def test_comma_separated_prose_list(index):
    lines = [
        "(3) Excludes Bank of America Corporation, Walmart Inc., Target Corporation, and Costco Wholesale Corporation.",
        "Our compensation peer group for fiscal 2025 consisted of Target Corporation, Walmart Inc., Costco Wholesale "
        "Corporation, The Kroger Co. and Best Buy Co., Inc., each of which competes with us for executive talent.",
    ]
    group = extract_peer_group(lines, index, target_ticker="HD")
    assert [p.ticker for p in group.peers] == ["TGT", "WMT", "COST", "KR", "BBY"]
    assert group.confidence is Confidence.MEDIUM


def test_no_recognisable_list_raises(index):
    lines = ["We benchmark against a peer group of similarly sized companies.", "Salary", "Bonus", "Total"]
    with pytest.raises(PeerGroupNotFoundError, match="no list of peer companies"):
        extract_peer_group(lines, index, target_ticker="HD")


def test_confidence_levels():
    assert confidence_for(8, 0) is Confidence.HIGH
    assert confidence_for(8, 9) is Confidence.MEDIUM
    assert confidence_for(4, 0) is Confidence.MEDIUM
    assert confidence_for(3, 0) is Confidence.LOW


# --- Fixture source -------------------------------------------------------


def group_for(ticker, peers, **kw):
    return ProxyPeerGroup(
        ticker=ticker,
        filing_date=kw.get("filing_date", date(2026, 4, 7)),
        accession_number="0000354950-26-000090",
        document_url="https://www.sec.gov/Archives/edgar/data/354950/000035495026000090/hd-20260406.htm",
        peers=[ProxyPeer(name=n, ticker=t) for t, n in peers],
        unmatched=kw.get("unmatched", []),
        confidence=kw.get("confidence", Confidence.HIGH),
        context="Retail Peer Group",
    )


def test_fixture_source_returns_copies_and_canned_failures():
    hd = group_for("HD", [("LOW", "Lowe's Companies, Inc.")])
    source = FixtureProxyPeerSource({"hd": hd, "NKE": EdgarUnavailableError("NKE", "EDGAR is down")})
    got = source.proxy_peer_group("HD")
    assert got == hd and got is not hd and got.proxy_year == 2026
    with pytest.raises(EdgarUnavailableError):
        source.proxy_peer_group("nke")
    with pytest.raises(ProxyFilingNotFoundError):
        source.proxy_peer_group("LOW")
    assert source.calls == ["HD", "NKE", "LOW"]


# --- EDGAR source over a fake client ---------------------------------------


class FakeClient:
    def __init__(self, tickers, submissions_by_cik, documents):
        self.tickers = tickers
        self.submissions = submissions_by_cik
        self.documents = documents
        self.calls: list[str] = []

    def get_json(self, url, *, ticker="EDGAR"):
        self.calls.append(url)
        if url == COMPANY_TICKERS_URL:
            return self.tickers
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        try:
            return self.submissions[cik]
        except KeyError:
            raise ProxyFilingNotFoundError(ticker, f"EDGAR has nothing at {url}") from None

    def get_text(self, url, *, ticker="EDGAR"):
        self.calls.append(url)
        return self.documents[url]


HD_URL = "https://www.sec.gov/Archives/edgar/data/354950/000035495026000090/hd-20260406.htm"


@pytest.fixture
def edgar(tickers_payload):
    client = FakeClient(
        tickers_payload,
        {
            354950: HD_SUBMISSIONS,
            60667: submissions([("10-K", "0000060667-26-000010", "2026-03-24", "low-20260130.htm")], name="LOWES COMPANIES INC"),
            320187: submissions([("DEF 14A", "0000320187-26-000089", "2026-07-15", "nke-20260715.htm")], name="NIKE, Inc."),
        },
        {
            HD_URL: (FIXTURES / "HD.html").read_text(),
            "https://www.sec.gov/Archives/edgar/data/320187/000032018726000089/nke-20260715.htm": "<p>No peer group here.</p>",
        },
    )
    now = [1000.0]
    return client, EdgarProxyPeerSource(client=client, clock=lambda: now[0]), now


def test_edgar_source_builds_a_group_with_provenance(edgar):
    client, source, _ = edgar
    group = source.proxy_peer_group("hd")
    assert group.ticker == "HD" and group.company_name == "HOME DEPOT, INC."
    assert (group.filing_date, group.accession_number, group.document_url) == (date(2026, 4, 7), "0000354950-26-000090", HD_URL)
    assert [p.ticker for p in group.peers][:3] == ["AMZN", "ROST", "AZO"] and group.confidence is Confidence.HIGH
    assert client.calls == [COMPANY_TICKERS_URL, "https://data.sec.gov/submissions/CIK0000354950.json", HD_URL]


def test_edgar_source_caches_results_and_the_index_until_ttl(edgar):
    client, source, now = edgar
    first = source.proxy_peer_group("HD")
    again = source.proxy_peer_group("HD")
    assert again == first and again is not first and len(client.calls) == 3
    now[0] += 25 * 3600
    source.proxy_peer_group("HD")
    assert len(client.calls) == 6  # index and filing both refetched


def test_edgar_source_failures_are_typed(edgar):
    _, source, _ = edgar
    with pytest.raises(ProxyFilingNotFoundError, match="not in the SEC's company list"):
        source.proxy_peer_group("ZZZZNOPE")
    with pytest.raises(ProxyFilingNotFoundError, match="no DEF 14A on file"):
        source.proxy_peer_group("LOW")
    with pytest.raises(PeerGroupNotFoundError):
        source.proxy_peer_group("NKE")


# --- EDGAR client ---------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code, self.text = status_code, text

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, *responses):
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_client_sets_edgar_headers_from_env(monkeypatch):
    monkeypatch.delenv(USER_AGENT_ENV, raising=False)
    session = FakeSession()
    EdgarClient(session=session)
    assert session.headers["User-Agent"] == DEFAULT_USER_AGENT and session.headers["Accept-Encoding"] == "gzip, deflate"
    monkeypatch.setenv(USER_AGENT_ENV, "Comps Research jonah@example.com")
    session = FakeSession()
    EdgarClient(session=session)
    assert session.headers["User-Agent"] == "Comps Research jonah@example.com"


def test_client_maps_http_failures_to_typed_errors():
    session = FakeSession(
        FakeResponse(200, '{"a": 1}'),
        FakeResponse(404),
        FakeResponse(403, "Request Rate Threshold Exceeded"),
        requests.ConnectionError("dns failed"),
        FakeResponse(200, "not json"),
    )
    client = EdgarClient(user_agent="t t@example.com", session=session, timeout=7.0, min_interval=0.0)
    assert client.get_json("https://data.sec.gov/x") == {"a": 1}
    assert session.calls[0] == ("https://data.sec.gov/x", 7.0)
    with pytest.raises(ProxyFilingNotFoundError):
        client.get_json("https://data.sec.gov/missing", ticker="HD")
    with pytest.raises(EdgarUnavailableError, match="HTTP 403"):
        client.get_text("https://www.sec.gov/y", ticker="HD")
    with pytest.raises(EdgarUnavailableError, match="dns failed"):
        client.get_text("https://www.sec.gov/z", ticker="HD")
    with pytest.raises(EdgarUnavailableError, match="malformed JSON"):
        client.get_json("https://www.sec.gov/w", ticker="HD")


def test_client_spaces_requests_to_respect_the_rate_limit():
    slept: list[float] = []
    session = FakeSession(FakeResponse(200, "a"), FakeResponse(200, "b"))
    client = EdgarClient(user_agent="t t@example.com", session=session, min_interval=0.12, clock=lambda: 100.0, sleep=slept.append)
    client.get_text("https://www.sec.gov/1")
    client.get_text("https://www.sec.gov/2")
    assert slept == [pytest.approx(0.12)]
