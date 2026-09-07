"""Endpoint tests over fixture providers: response shapes, HTTP status
mapping for each failure class, request validation, and the startup
cache wrap. The comps smoke test over recorded real data lives in
test_api_smoke.py.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.data import CachedProvider, CompanyProfile, FixtureProvider, MarketDataError, MarketDataProvider
from app.data.fixtures import HOME_DEPOT
from app.main import create_app
from tests.helpers import CountingProvider, recorded_provider

TODAY = date(2026, 9, 6)


def hd_like(ticker, **update):
    return HOME_DEPOT.model_copy(update={"ticker": ticker, "name": ticker, **update}, deep=True)


@pytest.fixture
def provider():
    return CountingProvider(
        {
            "HD": HOME_DEPOT,
            "LOW": hd_like("LOW", share_price=200.0),
            # ADR-style: quoted in USD, financials in CAD; the guard refuses it.
            "CAD": hd_like("CAD", price_currency="USD", reporting_currency="CAD"),
            "THIN": hd_like("THIN", quarters=HOME_DEPOT.quarters[:2]),
        }
    )


@pytest.fixture
def client(provider):
    app = create_app(provider=provider, today=lambda: TODAY)
    with TestClient(app) as c:
        yield c


# --- Startup -----------------------------------------------------------------


def test_startup_wraps_provider_in_cache(provider):
    app = create_app(provider=provider, today=lambda: TODAY)
    with TestClient(app) as c:
        assert isinstance(app.state.provider, CachedProvider)
        assert c.get("/api/company/HD").status_code == 200
        assert c.get("/api/company/HD").status_code == 200
    assert provider.fetch_calls == ["HD"]  # second call served from cache


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


# --- GET /api/company/{ticker} ---------------------------------------------


def test_company_snapshot_shape(client):
    r = client.get("/api/company/hd")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "HD"
    assert body["name"] == "The Home Depot, Inc."
    assert body["share_price"] == 321.05
    assert body["price_as_of"] == "2026-09-04"
    assert body["bridge"]["enterprise_value"] == pytest.approx(370_576.8e6, rel=1e-6)
    assert body["bridge"]["diluted_shares"] == 996e6
    assert body["ltm"]["method"] == "quarterly"
    assert body["ltm"]["ebitda"] == pytest.approx(25_333e6, rel=1e-6)
    assert body["multiples"]["ev_ebitda"]["value"] == pytest.approx(14.628, abs=5e-4)
    assert body["multiples"]["ev_ebitda"]["is_nm"] is False
    assert body["diluted_eps"] == pytest.approx(14_227 / 996, rel=1e-6)
    assert body["flags"] == []


def test_company_flags_are_returned(client):
    app = create_app(
        provider=FixtureProvider({"X": hd_like("X", diluted_shares=None, basic_shares=1_000e6)}),
        today=lambda: TODAY,
    )
    with TestClient(app) as c:
        body = c.get("/api/company/X").json()
    assert [f["code"] for f in body["flags"]] == ["basic_shares_fallback"]
    assert body["bridge"]["shares_are_basic_fallback"] is True


def test_company_not_found(client):
    r = client.get("/api/company/ZZZZNOPE")
    assert r.status_code == 404
    assert r.json() == {
        "ticker": "ZZZZNOPE",
        "code": "ticker_not_found",
        "message": "No market data found for ticker 'ZZZZNOPE'",
    }


def test_company_currency_mismatch_is_422(client):
    r = client.get("/api/company/CAD")
    assert r.status_code == 422
    assert r.json()["code"] == "currency_mismatch"


def test_company_insufficient_data_is_422(client):
    r = client.get("/api/company/THIN")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "insufficient_data"
    assert body["ticker"] == "THIN"


def test_company_source_failure_is_502():
    class Down(MarketDataProvider):
        def fetch_company(self, ticker):
            raise MarketDataError(f"{ticker}: yfinance request failed: timeout")

    with TestClient(create_app(provider=Down())) as c:
        r = c.get("/api/company/HD")
    assert r.status_code == 502
    assert r.json()["code"] == "source_error"


def test_company_rejects_malformed_ticker(client):
    assert client.get("/api/company/NOT%20A%20TICKER!").status_code == 422


# --- POST /api/comps ---------------------------------------------------------


def test_comps_shape_and_errors(client):
    r = client.post("/api/comps", json={"target": "HD", "peers": ["LOW", "ZZZZNOPE"]})
    assert r.status_code == 200
    body = r.json()
    assert body["as_of"] == "2026-09-06"
    assert body["target"]["ticker"] == "HD"
    assert [p["ticker"] for p in body["peers"]] == ["LOW"]
    assert set(body["statistics"]) == {"ev_revenue", "ev_ebitda", "ev_ebit", "pe"}
    assert body["statistics"]["ev_ebitda"]["n"] == 1
    assert body["implied"]["mid_basis"] == "median"
    assert body["implied"]["ev_ebitda"]["kind"] == "ev_ebitda"
    assert body["implied"]["ev_ebitda"]["is_nm"] is False
    assert body["errors"] == {
        "ZZZZNOPE": {
            "ticker": "ZZZZNOPE",
            "code": "ticker_not_found",
            "message": "No market data found for ticker 'ZZZZNOPE'",
        }
    }
    assert "flags" in body["target"] and all("flags" in p for p in body["peers"])


def test_comps_mean_basis(client):
    body = client.post("/api/comps", json={"target": "HD", "peers": ["LOW"], "mid_basis": "mean"}).json()
    assert body["implied"]["mid_basis"] == "mean"


def test_comps_target_not_found_is_404(client):
    r = client.post("/api/comps", json={"target": "ZZZZNOPE", "peers": ["LOW"]})
    assert r.status_code == 404
    assert r.json()["code"] == "ticker_not_found"


def test_comps_target_insufficient_data_is_422(client):
    r = client.post("/api/comps", json={"target": "THIN", "peers": ["LOW"]})
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_data"


@pytest.mark.parametrize(
    "payload",
    [
        {"target": "HD", "peers": []},
        {"target": "HD"},
        {"peers": ["LOW"]},
        {"target": "HD", "peers": ["NOT A TICKER"]},
        {"target": "HD", "peers": ["LOW"], "mid_basis": "mode"},
        {"target": "HD", "peers": ["LOW"] * 31},
    ],
)
def test_comps_request_validation(client, payload):
    r = client.post("/api/comps", json=payload)
    assert r.status_code == 422
    body = r.json()
    # Same body shape as every other error, so a generated client parses it.
    assert set(body) == {"ticker", "code", "message"}
    assert body["code"] == "invalid_request"
    assert body["ticker"] is None
    assert body["message"]


def test_malformed_path_ticker_uses_error_body(client):
    body = client.get("/api/company/NOT%20A%20TICKER!").json()
    assert body["code"] == "invalid_request"


def test_comps_with_only_the_target_as_peer_is_empty_not_an_error(client):
    r = client.post("/api/comps", json={"target": "HD", "peers": ["hd"]})
    assert r.status_code == 200
    body = r.json()
    assert body["peers"] == [] and body["errors"] == {}
    assert all(body["statistics"][k]["n"] == 0 for k in body["statistics"])
    assert all(body["implied"][k]["is_nm"] for k in ("ev_revenue", "ev_ebitda", "ev_ebit", "pe"))


# --- GET /api/peers/{ticker} ------------------------------------------------


def test_peers_shape():
    app = create_app(provider=recorded_provider(), today=lambda: TODAY)
    with TestClient(app) as c:
        r = c.get("/api/peers/hd")
    assert r.status_code == 200
    body = r.json()
    assert body["target"]["ticker"] == "HD"
    assert body["target"]["industry"] == "Home Improvement Retail"
    assert body["market_cap_band"]["low_multiple"] == 0.33
    assert body["market_cap_band"]["high_multiple"] == 3.0
    assert body["market_cap_band"]["low"] == pytest.approx(0.33 * 320308248576)
    # Recorded sector names (TSCO $18B, WSM $27B) sit below the band, so
    # widening adds nothing and the lone name carries both flags.
    assert [(p["ticker"], p["match_basis"]) for p in body["peers"]] == [("LOW", "industry")]
    assert [f["code"] for f in body["flags"]] == ["peer_set_widened", "thin_peer_set"]


def test_peers_small_target_widens_to_sector_and_flags():
    # FND ($5.3B): nothing else in Home Improvement Retail fits the band,
    # so a Specialty Retail name in the sector and the band fills in.
    arhs = CompanyProfile(
        ticker="ARHS", name="Arhaus, Inc.", sector="Consumer Cyclical", industry="Specialty Retail",
        market_cap=8e9, price_currency="USD", reporting_currency="USD",
    )
    app = create_app(provider=recorded_provider(ARHS=arhs), today=lambda: TODAY)
    with TestClient(app) as c:
        body = c.get("/api/peers/FND").json()
    assert [(p["ticker"], p["match_basis"]) for p in body["peers"]] == [("ARHS", "sector")]
    assert [f["code"] for f in body["flags"]] == ["peer_set_widened", "thin_peer_set"]


def test_peers_not_found():
    with TestClient(create_app(provider=recorded_provider())) as c:
        r = c.get("/api/peers/ZZZZNOPE")
    assert r.status_code == 404
    assert r.json()["code"] == "ticker_not_found"


def test_peers_unavailable_from_source_is_501(client):
    r = client.get("/api/peers/HD")  # CountingProvider has no discovery
    assert r.status_code == 501
    assert r.json()["code"] == "peer_discovery_unavailable"


def test_peers_without_market_cap_is_422():
    nocap = CompanyProfile(ticker="X", sector="Energy", industry="Oil & Gas Integrated", market_cap=None)
    with TestClient(create_app(provider=recorded_provider(X=nocap))) as c:
        r = c.get("/api/peers/X")
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_data"


# --- OpenAPI ---------------------------------------------------------------


def test_openapi_document_builds(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {"/api/health", "/api/company/{ticker}", "/api/comps", "/api/peers/{ticker}"}
    comps = spec["paths"]["/api/comps"]["post"]
    assert "200" in comps["responses"] and "404" in comps["responses"] and "502" in comps["responses"]
    assert "CompsResult" in spec["components"]["schemas"]
    assert "TickerError" in spec["components"]["schemas"]
    # Every 422, validation included, is a TickerError; the FastAPI default
    # validation schema must not be advertised alongside it.
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    ref = comps["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/TickerError")


# --- Frontend ----------------------------------------------------------------


def test_root_serves_the_frontend_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Comps Valuation Engine</title>" in r.text


def test_api_routes_win_over_the_frontend_mount(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/company/HD").status_code == 200
