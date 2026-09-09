"""Tests for the peer suggestion in the service layer: the proxy peer
group (filtered for business comparability) and the industry screen
are combined, not ranked. Every candidate is tagged with the source(s)
that named it, names in both come first, and every removed or unmapped
proxy name is a flag. The screen's own rule is covered in
test_services.py; this file covers the proxy step, its filter, the
union, and the labelling.
"""

from datetime import date

import pytest

from app.data import CompanyProfile, FixtureProvider, TickerNotFoundError
from app.data.fixtures import HOME_DEPOT
from app.data.proxy_peers import (
    Confidence,
    EdgarUnavailableError,
    FixtureProxyPeerSource,
    PeerGroupNotFoundError,
    ProxyFilingNotFoundError,
    ProxyPeer,
    ProxyPeerGroup,
)
from app.services import PeerSource, ProxyDropRule, suggest_peers
from app.services.peers import MARGIN_BAND
from app.valuation.models import FlagCode

B = 1_000_000_000.0
TODAY = date(2026, 9, 6)
HD_MARGIN = 25_333 / 169_176  # the fixture's LTM EBITDA / revenue, 14.97%
HD_PROXY_URL = "https://www.sec.gov/Archives/edgar/data/354950/000035495026000090/hd-20260406.htm"
PROXY_LABEL_HD = (
    "Compensation peer group from HD's 2026 proxy statement — disclosed for pay benchmarking, "
    "not valuation. Filter to business comparables before relying on the output."
)
SCREEN_LABEL_HD = (
    "Companies Yahoo files under Home Improvement Retail with a market cap 0.2x–5.0x HD's, in its currencies. "
    "Industry classifications are coarse; a screen match is a candidate, not a peer."
)
BOTH = [PeerSource.PROXY, PeerSource.SCREEN]
PROXY = [PeerSource.PROXY]
SCREEN = [PeerSource.SCREEN]


def profile(ticker, cap, *, industry="Home Improvement Retail", sector="Consumer Cyclical", currency="USD"):
    return CompanyProfile(
        ticker=ticker, name=f"{ticker} Inc.", sector=sector, industry=industry, market_cap=cap,
        price_currency=currency, reporting_currency=currency,
    )


def snapshot(ticker, margin=None, **update):
    """An HD-shaped snapshot; margin rewrites each quarter's EBIT so that
    LTM EBITDA / revenue equals it exactly."""
    snap = HOME_DEPOT.model_copy(update={"ticker": ticker, "name": f"{ticker} Inc.", **update}, deep=True)
    if margin is not None:
        snap.quarters = [q.model_copy(update={"ebit": margin * q.revenue - q.d_and_a}) for q in snap.quarters]
    return snap


class ScreenCounter(FixtureProvider):
    def __init__(self, snapshots, profiles):
        super().__init__(snapshots, profiles=profiles)
        self.screens = []

    def screen_peers(self, **kw):
        self.screens.append((kw["industry"], kw["sector"]))
        return super().screen_peers(**kw)


def group(peers, *, unmatched=(), confidence=Confidence.HIGH, ticker="HD", filing_date=date(2026, 4, 7)):
    return ProxyPeerGroup(
        ticker=ticker,
        company_name="HOME DEPOT, INC.",
        filing_date=filing_date,
        accession_number="0000354950-26-000090",
        document_url=HD_PROXY_URL,
        peers=[ProxyPeer(name=name, ticker=t) for t, name in peers],
        unmatched=list(unmatched),
        confidence=confidence,
        context="Retail Peer Group",
    )


DEFENSIVE = dict(industry="Discount Stores", sector="Consumer Defensive")
NO_REVENUE = [q.model_copy(update={"revenue": None}) for q in HOME_DEPOT.quarters]


@pytest.fixture
def provider():
    # HD's industry screen (Home Improvement Retail, 64B-1,600B) finds
    # LOW alone: THINT sits under the band, and the rest are other
    # industries. LOW is also in every proxy list below, so it is the
    # name both sources agree on. HD's margin is 14.97%, so the proxy
    # filter's band is 7.49%-22.46%.
    return ScreenCounter(
        {
            "HD": snapshot("HD"),
            "LOW": snapshot("LOW", margin=0.14),
            "ROST": snapshot("ROST", margin=0.13),
            "TJX": snapshot("TJX", margin=0.12),
            "TSCO": snapshot("TSCO", margin=0.12),
            "MCD": snapshot("MCD", margin=0.50),
            "BBY": snapshot("BBY", margin=0.04),  # under 7.49%: outside the band
            "ORLY": snapshot("ORLY", quarters=HOME_DEPOT.quarters[:2]),  # too few quarters
            "NOMARG": snapshot("NOMARG", quarters=NO_REVENUE),
            "THINT": snapshot("THINT", quarters=HOME_DEPOT.quarters[:2]),
            "WMT": snapshot("WMT", margin=0.06),
        },
        {
            "HD": profile("HD", 320 * B),
            "LOW": profile("LOW", 115 * B),
            "ROST": profile("ROST", 74 * B, industry="Apparel Retail"),
            "TJX": profile("TJX", 142 * B, industry="Apparel Retail"),
            "TSCO": profile("TSCO", 18 * B, industry="Specialty Retail"),
            "MCD": profile("MCD", 220 * B, industry="Restaurants"),
            "BBY": profile("BBY", 19 * B, industry="Specialty Retail"),
            "AZO": profile("AZO", 69 * B, industry="Auto Parts"),      # profile but no snapshot
            "ORLY": profile("ORLY", 70 * B, industry="Auto Parts"),
            "NOMARG": profile("NOMARG", 20 * B, industry="Specialty Retail"),
            "THINT": profile("THINT", 50 * B),
            "WMT": profile("WMT", 800 * B, **DEFENSIVE),
            "TGT": profile("TGT", 50 * B, **DEFENSIVE),
            "COST": profile("COST", 400 * B, **DEFENSIVE),
            "KR": profile("KR", 45 * B, industry="Grocery Stores", sector="Consumer Defensive"),
            "UL": profile("UL", 150 * B, industry="Household Products", sector="Consumer Defensive", currency="GBP"),
        },
    )


HD_RETAIL = [
    ("WMT", "Walmart Inc."),
    ("LOW", "Lowe’s Companies, Inc."),
    ("TGT", "Target Corporation"),
    ("COST", "Costco Wholesale Corporation"),
    ("KR", "The Kroger Co."),
    ("MCD", "McDonald's Corporation"),
    ("ROST", "Ross Stores, Inc."),
    ("TJX", "The TJX Companies, Inc."),
    ("BBY", "Best Buy Co., Inc."),
]
COMPARABLES = [("LOW", "Lowe’s Companies, Inc."), ("ROST", "Ross Stores, Inc."), ("TJX", "The TJX Companies, Inc."), ("TSCO", "Tractor Supply Company")]


def suggest(provider, source, **kw):
    return suggest_peers(provider, "hd", proxy_source=source, today=TODAY, **kw)


def tagged(result):
    return [(p.ticker, p.sources) for p in result.peers]


# --- The union ----------------------------------------------------------------


def test_candidates_are_the_union_of_both_sources_tagged_and_ordered(provider):
    source = FixtureProxyPeerSource({"HD": group(HD_RETAIL)})
    result = suggest(provider, source)
    assert result.proxy_label == PROXY_LABEL_HD and result.screen_label == SCREEN_LABEL_HD
    # LOW is in both (strongest, first); ROST and TJX passed the filter
    # and keep the filer's order; every name carries the margin it passed on.
    assert tagged(result) == [("LOW", BOTH), ("ROST", PROXY), ("TJX", PROXY)]
    assert [round(p.ebitda_margin, 2) for p in result.peers] == [0.14, 0.13, 0.12]
    assert result.peers[0].name == "LOW Inc." and result.peers[0].market_cap == 115 * B  # profiled, not just named
    # The rule that was applied.
    assert result.proxy_filter.sector == "Consumer Cyclical"
    assert result.proxy_filter.ebitda_margin == pytest.approx(HD_MARGIN)
    assert result.proxy_filter.margin_band == MARGIN_BAND == 0.5
    assert (result.proxy_filter.margin_low, result.proxy_filter.margin_high) == (pytest.approx(HD_MARGIN * 0.5), pytest.approx(HD_MARGIN * 1.5))
    # Every removal, with its reason.
    assert [(d.ticker, d.rule) for d in result.proxy_dropped] == [
        ("WMT", ProxyDropRule.SECTOR), ("TGT", ProxyDropRule.SECTOR), ("COST", ProxyDropRule.SECTOR),
        ("KR", ProxyDropRule.SECTOR), ("MCD", ProxyDropRule.MARGIN), ("BBY", ProxyDropRule.MARGIN),
    ]
    by_ticker = {d.ticker: d for d in result.proxy_dropped}
    assert by_ticker["WMT"].reason == "Consumer Defensive sector (Discount Stores), not Consumer Cyclical"
    assert by_ticker["WMT"].sector == "Consumer Defensive" and by_ticker["WMT"].ebitda_margin is None
    assert by_ticker["MCD"].reason == "EBITDA margin 50.0% against the target's 15.0%, outside 7.5%–22.5% (within 50% of the target's)"
    assert by_ticker["MCD"].ebitda_margin == pytest.approx(0.50) and by_ticker["MCD"].industry == "Restaurants"
    assert by_ticker["BBY"].reason.startswith("EBITDA margin 4.0% against the target's 15.0%, outside 7.5%–22.5%")
    assert [f.code for f in result.flags] == [FlagCode.PROXY_PEERS_DROPPED, FlagCode.THIN_PEER_SET]
    assert "6 of 9 compensation peers" in result.flags[0].message
    assert "outside the sector: 4, EBITDA margin too far: 2" in result.flags[0].message
    assert result.flags[1].message == (
        "Only 3 candidates across both sources (3 passed the proxy peer group's comparability filter, "
        "1 came from the screen); fewer than 4 is a thin set. Add peers by hand."
    )
    assert result.proxy.document_url == HD_PROXY_URL and source.calls == ["HD"]
    assert provider.screens == [("Home Improvement Retail", "Consumer Cyclical")]


def test_screen_only_names_follow_the_proxy_names_closest_in_size_first(provider):
    provider._profiles["BIG"] = profile("BIG", 600 * B)   # ratio 1.9
    provider._profiles["NEAR"] = profile("NEAR", 300 * B)  # ratio 0.94: nearest to HD
    result = suggest(provider, FixtureProxyPeerSource({"HD": group(COMPARABLES)}))
    assert tagged(result) == [
        ("LOW", BOTH), ("ROST", PROXY), ("TJX", PROXY), ("TSCO", PROXY), ("NEAR", SCREEN), ("BIG", SCREEN),
    ]
    assert result.peers[4].ebitda_margin is None  # screen names are not valued at suggestion time
    assert result.flags == []


def test_a_proxy_name_the_filter_removed_can_still_arrive_through_the_screen(provider):
    # NEAR is in HD's industry and band but its margin is far off: the
    # proxy filter drops it (and says so), the screen names it anyway.
    # Both facts are reported; the reader decides.
    provider._profiles["NEAR"] = profile("NEAR", 300 * B)
    provider._snapshots["NEAR"] = snapshot("NEAR", margin=0.40)
    result = suggest(provider, FixtureProxyPeerSource({"HD": group(COMPARABLES + [("NEAR", "Near Inc.")])}))
    assert tagged(result)[-1] == ("NEAR", SCREEN)
    assert [(d.ticker, d.rule) for d in result.proxy_dropped] == [("NEAR", ProxyDropRule.MARGIN)]


def test_the_filer_itself_is_never_its_own_candidate(provider):
    result = suggest(provider, FixtureProxyPeerSource({"HD": group([("HD", "The Home Depot, Inc.")] + COMPARABLES)}))
    assert "HD" not in {p.ticker for p in result.peers} and result.proxy_dropped == []
    assert tagged(result)[0] == ("LOW", BOTH)


def test_limit_applies_to_the_ordered_union(provider):
    result = suggest(provider, FixtureProxyPeerSource({"HD": group(COMPARABLES)}), limit=2)
    assert tagged(result) == [("LOW", BOTH), ("ROST", PROXY)]
    assert [f.code for f in result.flags] == [FlagCode.THIN_PEER_SET]


# --- The comparability filter ---------------------------------------------------


def test_unvaluable_and_marginless_names_are_dropped_with_reasons(provider):
    peers = COMPARABLES + [("AZO", "AutoZone, Inc."), ("ORLY", "O’Reilly Automotive, Inc."), ("UL", "Unilever PLC"), ("NOMARG", "Nomarg Corp.")]
    result = suggest(provider, FixtureProxyPeerSource({"HD": group(peers, unmatched=["adidas AG", "Bunnings Group"])}))
    assert [p.ticker for p in result.peers] == ["LOW", "ROST", "TJX", "TSCO"]
    assert [(d.ticker, d.rule) for d in result.proxy_dropped] == [
        ("AZO", ProxyDropRule.UNVALUABLE), ("ORLY", ProxyDropRule.UNVALUABLE),
        ("UL", ProxyDropRule.UNVALUABLE), ("NOMARG", ProxyDropRule.NO_MARGIN),
    ]
    by_ticker = {d.ticker: d for d in result.proxy_dropped}
    assert "No market data found for ticker 'AZO'" in by_ticker["AZO"].reason
    assert "GBP" in by_ticker["UL"].reason and by_ticker["UL"].sector == "Consumer Defensive"
    assert by_ticker["NOMARG"].reason == "LTM EBITDA margin unavailable (revenue or EBITDA not reported)"
    assert [f.code for f in result.flags] == [FlagCode.PROXY_PEERS_UNMATCHED, FlagCode.PROXY_PEERS_DROPPED]
    assert "2 names" in result.flags[0].message and "adidas AG, Bunnings Group" in result.flags[0].message
    assert "4 of 8 compensation peers" in result.flags[1].message and "(no margin: 1, cannot be valued: 3)" in result.flags[1].message


def test_margin_band_is_half_the_targets_margin_either_way(provider):
    provider._snapshots["HI"] = snapshot("HI", margin=HD_MARGIN * 1.49)
    provider._snapshots["LO"] = snapshot("LO", margin=HD_MARGIN * 0.51)
    provider._snapshots["FAR"] = snapshot("FAR", margin=HD_MARGIN * 1.51)
    provider._snapshots["NEG"] = snapshot("NEG", margin=-0.03)
    for t in ("HI", "LO", "FAR", "NEG"):
        provider._profiles[t] = profile(t, 100 * B, industry="Apparel Retail")
    result = suggest(provider, FixtureProxyPeerSource({"HD": group([("HI", "Hi"), ("LO", "Lo"), ("FAR", "Far"), ("NEG", "Neg"), ("LOW", "Low")])}))
    assert [p.ticker for p in result.peers] == ["LOW", "HI", "LO"]  # LOW first: both sources name it
    assert [(d.ticker, d.rule) for d in result.proxy_dropped] == [("FAR", ProxyDropRule.MARGIN), ("NEG", ProxyDropRule.MARGIN)]
    assert "EBITDA margin -3.0% against the target's 15.0%, outside 7.5%–22.5%" in result.proxy_dropped[1].reason


def test_margin_test_is_skipped_when_the_targets_margin_is_not_positive(provider):
    # A relative band around zero admits nothing, so the test is dropped
    # rather than misapplied, and the flag says so.
    provider._snapshots["NEGT"] = snapshot("NEGT", margin=-0.02)
    provider._profiles["NEGT"] = profile("NEGT", 100 * B)
    source = FixtureProxyPeerSource({"NEGT": group([("LOW", "Lowe's"), ("MCD", "McDonald's")], ticker="NEGT")})
    result = suggest_peers(provider, "NEGT", proxy_source=source, today=TODAY)
    assert {p.ticker for p in result.peers if PeerSource.PROXY in p.sources} == {"LOW", "MCD"}
    assert result.proxy_filter.ebitda_margin is None and result.proxy_filter.margin_low is None
    assert result.flags[0].code is FlagCode.PROXY_FILTER_PARTIAL
    assert "NEGT's EBITDA margin is -2.0%, not positive" in result.flags[0].message


def test_margin_test_is_skipped_when_the_target_has_no_margin(provider):
    source = FixtureProxyPeerSource({"THINT": group([("LOW", "Lowe's"), ("MCD", "McDonald's"), ("WMT", "Walmart")], ticker="THINT")})
    result = suggest_peers(provider, "THINT", proxy_source=source, today=TODAY)
    assert tagged(result) == [("LOW", BOTH), ("MCD", PROXY)]  # sector only: MCD's 50% margin is not tested
    assert result.peers[1].ebitda_margin is None and result.proxy_filter.ebitda_margin is None
    assert [(d.ticker, d.rule) for d in result.proxy_dropped] == [("WMT", ProxyDropRule.SECTOR)]
    assert [f.code for f in result.flags] == [FlagCode.PROXY_FILTER_PARTIAL, FlagCode.PROXY_PEERS_DROPPED, FlagCode.THIN_PEER_SET]
    assert "filtered on sector only" in result.flags[0].message


def test_proxy_year_comes_from_the_filing_date(provider):
    source = FixtureProxyPeerSource({"HD": group(COMPARABLES, filing_date=date(2025, 4, 7))})
    assert "HD's 2025 proxy statement" in suggest(provider, source).proxy_label


# --- The proxy step contributes nothing; the screen still runs -----------------


def test_nothing_surviving_the_filter_leaves_the_screen_and_the_drop_list(provider):
    all_defensive = group([("WMT", "Walmart"), ("TGT", "Target"), ("COST", "Costco"), ("KR", "Kroger")])
    result = suggest(provider, FixtureProxyPeerSource({"HD": all_defensive}))
    assert tagged(result) == [("LOW", SCREEN)]
    # The disclosure stays: the group was read and filtered, so the label,
    # the rule, and every removal remain on show for adding back.
    assert result.proxy_label == PROXY_LABEL_HD and result.proxy == all_defensive
    assert result.proxy_filter.sector == "Consumer Cyclical"
    assert [d.ticker for d in result.proxy_dropped] == ["WMT", "TGT", "COST", "KR"]
    assert [f.code for f in result.flags] == [
        FlagCode.PROXY_PEERS_DROPPED, FlagCode.PROXY_PEERS_UNAVAILABLE, FlagCode.THIN_PEER_SET,
    ]
    assert "none of the 4 compensation peers" in result.flags[1].message and "added back" in result.flags[1].message
    assert "Candidates come from the industry screen only" in result.flags[1].message
    assert "0 passed the proxy peer group's comparability filter, 1 came from the screen" in result.flags[2].message


def test_low_confidence_proxy_is_not_used_but_stays_visible(provider):
    weak = group(COMPARABLES[:2], unmatched=["adidas AG"], confidence=Confidence.LOW)
    result = suggest(provider, FixtureProxyPeerSource({"HD": weak}))
    assert tagged(result) == [("LOW", SCREEN)]
    assert result.proxy_label is None and result.proxy == weak
    assert result.proxy_dropped == [] and result.proxy_filter is None
    assert [f.code for f in result.flags] == [FlagCode.PROXY_PEERS_LOW_CONFIDENCE, FlagCode.THIN_PEER_SET]
    low = result.flags[0].message
    assert "only 2 names mapped" in low and "Lowe’s Companies, Inc., Ross Stores, Inc., adidas AG" in low and HD_PROXY_URL in low
    assert "industry screen only" in low
    assert "Only 1 Home Improvement Retail name within 0.2x-5.0x of HD's market cap" in result.flags[1].message
    assert provider.screens == [("Home Improvement Retail", "Consumer Cyclical")]


@pytest.mark.parametrize(
    "failure, detail",
    [
        (ProxyFilingNotFoundError("HD", "no DEF 14A on file for HOME DEPOT, INC."), "no DEF 14A on file"),
        (PeerGroupNotFoundError("HD", "no list of peer companies was recognised"), "no list of peer companies"),
        (EdgarUnavailableError("HD", "EDGAR returned HTTP 503"), "HTTP 503"),
        (RuntimeError("parser bug"), "RuntimeError: parser bug"),
    ],
)
def test_proxy_failures_are_flags_never_exceptions(provider, failure, detail):
    result = suggest(provider, FixtureProxyPeerSource({"HD": failure}))
    assert result.proxy is None and result.proxy_label is None and result.proxy_filter is None
    assert result.flags[0].code is FlagCode.PROXY_PEERS_UNAVAILABLE
    assert detail in result.flags[0].message and "industry screen only" in result.flags[0].message
    assert tagged(result) == [("LOW", SCREEN)]


def test_unknown_ticker_at_the_proxy_source_is_a_flag(provider):
    result = suggest(provider, FixtureProxyPeerSource({}))
    assert result.flags[0].code is FlagCode.PROXY_PEERS_UNAVAILABLE and "no proxy statement on file" in result.flags[0].message


# --- Without a proxy source ----------------------------------------------------


def test_without_a_proxy_source_only_the_screen_is_described(provider):
    result = suggest_peers(provider, "HD")
    assert tagged(result) == [("LOW", SCREEN)]
    assert result.proxy is None and result.proxy_label is None and result.proxy_filter is None
    assert result.proxy_dropped == [] and result.screen_label == SCREEN_LABEL_HD
    assert [f.code for f in result.flags] == [FlagCode.THIN_PEER_SET]


def test_target_errors_still_raise_before_the_proxy_step(provider):
    source = FixtureProxyPeerSource({"ZZZ": group(COMPARABLES, ticker="ZZZ")})
    with pytest.raises(TickerNotFoundError):
        suggest_peers(provider, "ZZZ", proxy_source=source)
    assert source.calls == []
