"""Live integration tests against SEC EDGAR: pull the latest DEF 14A for
Home Depot, Nike, and Lowe's and check the peer group parses to the
names each company disclosed, then run the full peer suggestion for HD
through Yahoo Finance to confirm the proxy step fires end to end.

Skipped unless COMPS_LIVE_TESTS=1 (network; EDGAR asks for a contact in
the User-Agent, set COMPS_SEC_USER_AGENT). Run with -s to see each group:

    COMPS_LIVE_TESTS=1 .venv/bin/pytest tests/test_edgar_live.py -s

The expected names are the 2026 proxies' lists. Companies revise their
peer groups each year, so the check is a majority overlap, not equality;
when a new proxy lands and the overlap drops, update the lists here.
"""

import os

import pytest

from app.data import CachedProvider
from app.data.edgar import EdgarProxyPeerSource
from app.data.proxy_peers import Confidence, ProxyFilingNotFoundError
from app.services import PeerSource, suggest_peers

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("COMPS_LIVE_TESTS"), reason="set COMPS_LIVE_TESTS=1 to hit EDGAR"),
]

DISCLOSED_2026 = {
    "HD": {"AMZN", "ROST", "AZO", "TGT", "COST", "KR", "LOW", "TJX", "ORLY", "WMT"},
    "NKE": {"BBY", "MSFT", "SBUX", "CSCO", "MDLZ", "TGT", "KO", "NFLX", "TJX", "KMB", "PEP", "WMT", "LOW", "PG", "DIS", "MCD", "CRM"},
    "LOW": {"BBY", "COST", "CVS", "DG", "NKE", "SBUX", "TGT", "HD", "KR", "TJX", "WMT"},
}


@pytest.fixture(scope="module")
def source():
    return EdgarProxyPeerSource()


@pytest.mark.parametrize("ticker", sorted(DISCLOSED_2026))
def test_proxy_peer_group_parses_from_edgar(source, ticker):
    group = source.proxy_peer_group(ticker)
    found = {p.ticker for p in group.peers}
    print(f"\n{ticker} {group.filing_date} {group.confidence.value} {group.context[:60]!r}")
    print("  peers:", ", ".join(f"{p.ticker} ({p.name})" for p in group.peers))
    print("  unmatched:", group.unmatched)
    assert group.filing_date.year >= 2026
    assert group.confidence is Confidence.HIGH
    expected = DISCLOSED_2026[ticker]
    assert len(found & expected) >= 0.7 * len(expected), f"overlap with the 2026 list dropped: {sorted(found)}"
    assert ticker not in found


def test_unknown_ticker_has_no_filing(source):
    with pytest.raises(ProxyFilingNotFoundError):
        source.proxy_peer_group("ZZZZNOPE")


def test_suggest_peers_combines_the_proxy_group_with_the_screen_for_home_depot(source):
    from app.data.yfinance_provider import YFinanceProvider

    result = suggest_peers(CachedProvider(YFinanceProvider()), "HD", proxy_source=source)
    assert result.proxy_label.startswith("Compensation peer group from HD's 20")
    assert result.screen_label.startswith("Companies Yahoo files under Home Improvement Retail")
    from_proxy = {p.ticker for p in result.peers if PeerSource.PROXY in p.sources}
    assert len(from_proxy) >= 4 and from_proxy >= {"LOW", "TJX"}
    # LOW is HD's one industry-screen match at its size, and a comp peer.
    assert next(p.sources for p in result.peers if p.ticker == "LOW") == [PeerSource.PROXY, PeerSource.SCREEN]
    assert result.peers[0].ticker == "LOW"  # names both sources agree on come first
    # Yahoo files discount stores under Consumer Defensive: the filter drops them, with reasons.
    assert {d.ticker for d in result.proxy_dropped} >= {"WMT", "TGT"}
    assert all(d.reason for d in result.proxy_dropped)
