"""GET /api/peers/{ticker} with a proxy peer source: the response carries
both sources' labels, the tagged union, and the proxy extraction, and
the app factory wires EDGAR in only for the default (production)
provider."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.data import CompanyProfile
from app.data.proxy_peers import Confidence, FixtureProxyPeerSource, ProxyPeer, ProxyPeerGroup
from app.main import PROXY_PEERS_ENV, _default_proxy_source, create_app
from tests.helpers import recorded_provider

TODAY = date(2026, 9, 6)
HD_PROXY_URL = "https://www.sec.gov/Archives/edgar/data/354950/000035495026000090/hd-20260406.htm"

# Recorded snapshots and profiles exist for HD, LOW, FND, TSCO, WSM, all
# Consumer Cyclical with LTM EBITDA margins of 11%-22% against HD's 15%,
# so all four pass the filter; an Energy name and an unknown ticker
# exercise the drops without the network. LOW is also the one recorded
# name HD's industry screen finds, so it lands in both sources.
XOM = CompanyProfile(
    ticker="XOM", name="Exxon Mobil Corporation", sector="Energy", industry="Oil & Gas Integrated",
    market_cap=500e9, price_currency="USD", reporting_currency="USD",
)
HD_GROUP = ProxyPeerGroup(
    ticker="HD",
    company_name="HOME DEPOT, INC.",
    filing_date=date(2026, 4, 7),
    accession_number="0000354950-26-000090",
    document_url=HD_PROXY_URL,
    peers=[
        ProxyPeer(name="Tractor Supply Company", ticker="TSCO"),
        ProxyPeer(name="Lowe’s Companies, Inc.", ticker="LOW"),
        ProxyPeer(name="Williams-Sonoma, Inc.", ticker="WSM"),
        ProxyPeer(name="Floor & Decor Holdings, Inc.", ticker="FND"),
        ProxyPeer(name="Exxon Mobil Corporation", ticker="XOM"),
        ProxyPeer(name="Gone Private Inc.", ticker="GONE"),
    ],
    unmatched=["Bunnings Group"],
    confidence=Confidence.MEDIUM,
    context="Retail Peer Group",
)


def test_peers_are_the_tagged_union_of_the_proxy_group_and_the_screen():
    app = create_app(provider=recorded_provider(XOM=XOM), proxy_source=FixtureProxyPeerSource({"HD": HD_GROUP}), today=lambda: TODAY)
    with TestClient(app) as c:
        r = c.get("/api/peers/hd")
    assert r.status_code == 200
    body = r.json()
    assert body["proxy_label"] == (
        "Compensation peer group from HD's 2026 proxy statement — disclosed for pay benchmarking, "
        "not valuation. Filter to business comparables before relying on the output."
    )
    assert body["screen_label"] == (
        "Companies Yahoo files under Home Improvement Retail with a market cap 0.2x–5.0x HD's, in its currencies. "
        "Industry classifications are coarse; a screen match is a candidate, not a peer."
    )
    # LOW first (both sources), then the rest of the proxy group in the filer's order.
    assert [(p["ticker"], p["sources"]) for p in body["peers"]] == [
        ("LOW", ["proxy", "screen"]), ("TSCO", ["proxy"]), ("WSM", ["proxy"]), ("FND", ["proxy"]),
    ]
    assert body["peers"][1]["name"] == "Tractor Supply Company" and body["peers"][1]["market_cap"] > 0
    assert [round(p["ebitda_margin"], 3) for p in body["peers"]] == [0.141, 0.121, 0.221, 0.114]
    assert body["proxy_filter"] == {
        "sector": "Consumer Cyclical", "ebitda_margin": pytest.approx(0.1497, abs=1e-3), "margin_band": 0.5,
        "margin_low": pytest.approx(0.0749, abs=1e-3), "margin_high": pytest.approx(0.2246, abs=1e-3),
    }
    assert [(d["ticker"], d["rule"]) for d in body["proxy_dropped"]] == [("XOM", "sector"), ("GONE", "unvaluable")]
    assert body["proxy_dropped"][0]["reason"] == "Energy sector (Oil & Gas Integrated), not Consumer Cyclical"
    assert body["proxy_dropped"][0]["name"] == "Exxon Mobil Corporation" and body["proxy_dropped"][0]["sector"] == "Energy"
    proxy = body["proxy"]
    assert proxy["filing_date"] == "2026-04-07" and proxy["document_url"] == HD_PROXY_URL
    assert proxy["confidence"] == "medium" and proxy["unmatched"] == ["Bunnings Group"]
    assert [p["ticker"] for p in proxy["peers"]] == ["TSCO", "LOW", "WSM", "FND", "XOM", "GONE"]  # the raw list stays
    assert [f["code"] for f in body["flags"]] == ["proxy_peers_unmatched", "proxy_peers_dropped"]


def test_peers_without_a_readable_proxy_come_from_the_screen_and_say_so():
    app = create_app(provider=recorded_provider(), proxy_source=FixtureProxyPeerSource({}), today=lambda: TODAY)
    with TestClient(app) as c:
        body = c.get("/api/peers/HD").json()
    assert body["proxy"] is None and body["proxy_label"] is None
    assert body["proxy_filter"] is None and body["proxy_dropped"] == []
    assert [f["code"] for f in body["flags"]] == ["proxy_peers_unavailable", "thin_peer_set"]
    assert "industry screen only" in body["flags"][0]["message"]
    assert [(p["ticker"], p["sources"]) for p in body["peers"]] == [("LOW", ["screen"])]


def test_peers_without_any_proxy_source_carry_no_proxy_flag():
    with TestClient(create_app(provider=recorded_provider(), today=lambda: TODAY)) as c:
        body = c.get("/api/peers/HD").json()
        assert c.app.state.proxy_source is None
    assert body["proxy"] is None and body["proxy_label"] is None
    assert [f["code"] for f in body["flags"]] == ["thin_peer_set"]


def test_default_proxy_source_is_edgar_unless_switched_off(monkeypatch):
    monkeypatch.delenv(PROXY_PEERS_ENV, raising=False)
    assert type(_default_proxy_source()).__name__ == "EdgarProxyPeerSource"
    for value in ("off", "0", "false", "NO"):
        monkeypatch.setenv(PROXY_PEERS_ENV, value)
        assert _default_proxy_source() is None


def test_openapi_documents_both_sources():
    with TestClient(create_app(provider=recorded_provider())) as c:
        spec = c.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["PeerSuggestions"]
    assert {"proxy_label", "screen_label", "proxy", "proxy_filter", "proxy_dropped", "peers", "flags"} <= set(schema["properties"])
    assert "source" not in schema["properties"] and "source_label" not in schema["properties"]
    assert "sources" in spec["components"]["schemas"]["SuggestedPeer"]["properties"]
    assert spec["components"]["schemas"]["ProxyDropRule"]["enum"] == ["sector", "margin", "no_margin", "unvaluable"]
    assert spec["components"]["schemas"]["PeerSource"]["enum"] == ["proxy", "screen"]
