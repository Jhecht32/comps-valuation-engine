"""Smoke test: a real comps request end to end through the API.

Home Depot against Lowe's, Floor & Decor, Tractor Supply, and
Williams-Sonoma, plus a deliberately bad ticker. The default run uses
snapshots recorded from Yahoo Finance (see tests/fixtures/), so it is
offline and deterministic; the live twin repeats it against Yahoo when
COMPS_LIVE_TESTS=1, asserting only what cannot drift.
"""

import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import RECORDED_AT, recorded_provider

TARGET = "HD"
PEERS = ["LOW", "FND", "TSCO", "WSM"]
BAD = "ZZZZNOPE"
REQUEST = {"target": TARGET, "peers": PEERS + [BAD]}
MULTIPLES = ("ev_revenue", "ev_ebitda", "ev_ebit", "pe")


def assert_comps_shape(body, *, expected_n):
    assert body["target"]["ticker"] == TARGET
    assert [p["ticker"] for p in body["peers"]] == PEERS

    for company in [body["target"], *body["peers"]]:
        assert set(company) >= {"ticker", "name", "share_price", "price_as_of", "bridge", "ltm", "multiples", "diluted_eps", "flags"}
        assert isinstance(company["flags"], list)
        assert company["bridge"]["enterprise_value"] > 0
        for key in MULTIPLES:
            m = company["multiples"][key]
            assert (m["value"] is None) == m["is_nm"]

    assert set(body["statistics"]) == set(MULTIPLES)
    for key in MULTIPLES:
        stats = body["statistics"][key]
        assert stats["n"] == expected_n[key]
        if stats["n"]:
            assert stats["min"] <= stats["p25"] <= stats["median"] <= stats["p75"] <= stats["max"]

    assert body["implied"]["mid_basis"] == "median"
    for key in MULTIPLES:
        rng = body["implied"][key]
        assert rng["kind"] == key
        assert rng["n"] == expected_n[key]
        if not rng["is_nm"]:
            assert rng["low"] <= rng["mid"] <= rng["high"]

    assert set(body["errors"]) == {BAD}
    assert body["errors"][BAD]["code"] == "ticker_not_found"
    assert body["errors"][BAD]["ticker"] == BAD


def test_comps_smoke_recorded():
    app = create_app(provider=recorded_provider(), today=lambda: RECORDED_AT)
    with TestClient(app) as client:
        r = client.post("/api/comps", json=REQUEST)
    assert r.status_code == 200, r.text
    body = r.json()

    # All four peers have positive revenue, EBITDA, EBIT and EPS in the
    # recorded window, so every multiple is meaningful for every peer.
    assert_comps_shape(body, expected_n={k: 4 for k in MULTIPLES})
    assert body["as_of"] == RECORDED_AT.isoformat()
    assert body["target"]["multiples"]["ev_ebitda"]["value"] == pytest.approx(14.628, abs=5e-4)
    assert body["target"]["flags"] == []
    assert all(p["flags"] == [] for p in body["peers"])


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("COMPS_LIVE_TESTS"), reason="set COMPS_LIVE_TESTS=1 to hit Yahoo")
def test_comps_smoke_live():
    from app.data.yfinance_provider import YFinanceProvider

    app = create_app(provider=YFinanceProvider())
    with TestClient(app) as client:
        r = client.post("/api/comps", json=REQUEST)
    assert r.status_code == 200, r.text
    body = r.json()
    # Revenue is positive for every peer whatever the quarter; the other
    # multiples may go NM as results move, so they are bounded, not pinned.
    n_rev = body["statistics"]["ev_revenue"]["n"]
    assert n_rev == 4
    assert_comps_shape(
        body,
        expected_n={k: body["statistics"][k]["n"] for k in MULTIPLES},
    )
    assert all(0 <= body["statistics"][k]["n"] <= 4 for k in MULTIPLES)
    assert body["as_of"] == date.today().isoformat()
