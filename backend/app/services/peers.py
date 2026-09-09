"""Candidate peers for a target, from two sources combined.

Neither source is a peer set on its own, so the suggestion is the union
of both, each name tagged with the source(s) that named it, for the
reader to curate.

1. Proxy peer group (COMP PEERS). The target's latest DEF 14A names
   the peer group its compensation committee benchmarks against. That
   is a sourced list, but it is a pay benchmark, not a valuation one:
   boards pick compensation peers for revenue scale and competition for
   talent, so Nike's names Microsoft, Cisco, and Kimberly-Clark, and
   Nike's business comparables (adidas, Puma, Deckers, Skechers) are not
   on it at all. The extracted list is filtered for business
   comparability: a peer is kept only if it shares the target's sector
   and its LTM EBITDA margin (the engine's own figure) is within
   MARGIN_BAND of the target's in relative terms. Every name removed is returned
   with its reason so it can be added back; names the SEC's company
   list cannot map to a ticker are reported too. The step contributes
   when the extraction reads at least MEDIUM confidence; survivors keep
   the filer's order and are not screened by size.
2. Industry screen (SCREEN). Companies Yahoo classifies in the target's
   industry, inside the market-cap band, in the target's currencies,
   closest in size first. Yahoo's industries are coarse ("Specialty
   Retail" spans auto parts and party supplies), so a screen match is a
   candidate, not a peer. A target with no industry classification is
   screened on its sector instead, flagged SCREEN_ON_SECTOR, since a
   sector like "Consumer Cyclical" puts restaurants and hotels next to
   a home-improvement retailer.

The union is ordered with the strongest signal first: names both
sources agree on, then the rest of the proxy group, then the rest of
the screen. A union still short of MIN_PEERS carries THIN_PEER_SET; a
single name is never returned unflagged. A proxy failure of any kind
(no filing, an unreadable list, EDGAR down, a parser bug) is a flag,
never an exception; the screen still runs.
"""

from datetime import date

from app.data.peers import (
    MARKET_CAP_HIGH_MULTIPLE,
    MARKET_CAP_LOW_MULTIPLE,
    MarketCapBand,
    MatchBasis,
    market_cap_band,
    same_currency,
    select_peers,
)
from app.data.provider import CompanyProfile, MarketDataError, MarketDataProvider
from app.data.proxy_peers import Confidence, ProxyPeer, ProxyPeerGroup, ProxyPeersError, ProxyPeerSource
from app.services.comps import normalise_ticker, value_company
from app.services.errors import InsufficientDataError
from app.services.models import (
    DroppedProxyPeer,
    PeerSource,
    PeerSuggestions,
    ProxyDropRule,
    ProxyFilter,
    SuggestedPeer,
)
from app.valuation import ebitda_margin
from app.valuation.models import DataQualityFlag, FlagCode

MIN_PEERS = 4  # fewer candidates than this, across both sources, is a thin set
DEFAULT_LIMIT = 25
MARGIN_BAND = 0.5  # a proxy peer's LTM EBITDA margin may sit this far from the target's, relative (9.9% admits 5.0%-14.9%)
_SCREEN_SIZE = 100  # ask the source for more than the limit; the rule drops some

PROXY_LABEL = (
    "Compensation peer group from {ticker}'s {year} proxy statement — disclosed for pay benchmarking, "
    "not valuation. Filter to business comparables before relying on the output."
)
SCREEN_LABEL = (
    "Companies Yahoo files under {industry} with a market cap {low}x–{high}x {ticker}'s, in its currencies. "
    "Industry classifications are coarse; a screen match is a candidate, not a peer."
)
SECTOR_SCREEN_LABEL = (
    "{ticker} has no industry classification, so the {sector} sector was screened instead (market cap "
    "{low}x–{high}x {ticker}'s, same currencies). Sector matches are loose; review each name."
)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _multiple(x: float) -> str:
    text = f"{x:.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text  # 0.33, 3.0


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# --- Screen ---------------------------------------------------------------


def _screen(
    provider: MarketDataProvider, target: CompanyProfile, band: MarketCapBand, basis: MatchBasis
) -> list[CompanyProfile]:
    candidates = provider.screen_peers(
        sector=target.sector,
        industry=target.industry if basis is MatchBasis.INDUSTRY else None,
        market_cap_min=band.low,
        market_cap_max=band.high,
        limit=_SCREEN_SIZE,
    )
    return select_peers(
        target,
        candidates,
        basis=basis,
        low_multiple=band.low_multiple,
        high_multiple=band.high_multiple,
    )


def _screen_label(target: CompanyProfile, band: MarketCapBand, basis: MatchBasis) -> str:
    template = SCREEN_LABEL if basis is MatchBasis.INDUSTRY else SECTOR_SCREEN_LABEL
    return template.format(
        ticker=target.ticker,
        industry=target.industry,
        sector=target.sector,
        low=_multiple(band.low_multiple),
        high=_multiple(band.high_multiple),
    )


def _sector_flag(target: CompanyProfile) -> DataQualityFlag:
    return DataQualityFlag(
        code=FlagCode.SCREEN_ON_SECTOR,
        message=(
            f"{target.ticker} has no industry classification, so the {target.sector} sector was screened instead. "
            "Sector matches are loose; review each screened name before relying on it."
        ),
    )


def _thin_flag(target: CompanyProfile, band: MarketCapBand, peers: list[SuggestedPeer], *, proxy_ran: bool) -> DataQualityFlag:
    screened = sum(1 for p in peers if PeerSource.SCREEN in p.sources)
    if proxy_ran:
        from_proxy = sum(1 for p in peers if PeerSource.PROXY in p.sources)
        found = (
            f"Only {_plural(len(peers), 'candidate')} across both sources ({from_proxy} passed the proxy peer "
            f"group's comparability filter, {screened} came from the screen)"
        )
    else:
        found = (
            f"Only {_plural(screened, target.industry + ' name' if target.industry else 'name')} within "
            f"{_multiple(band.low_multiple)}x-{_multiple(band.high_multiple)}x of {target.ticker}'s market cap"
        )
    return DataQualityFlag(
        code=FlagCode.THIN_PEER_SET,
        message=f"{found}; fewer than {MIN_PEERS} is a thin set. Add peers by hand.",
    )


# --- Proxy step -----------------------------------------------------------


def _proxy_label(target: CompanyProfile, group: ProxyPeerGroup) -> str:
    return PROXY_LABEL.format(ticker=target.ticker, year=group.proxy_year)


def _unavailable_flag(detail: str) -> DataQualityFlag:
    return DataQualityFlag(
        code=FlagCode.PROXY_PEERS_UNAVAILABLE,
        message=f"No proxy-statement peer group: {detail}. Candidates come from the industry screen only.",
    )


def _margin_of(provider: MarketDataProvider, ticker: str, today: date) -> tuple[float | None, str | None]:
    """The engine's LTM EBITDA margin for a ticker, or (None, why not)."""
    try:
        valued = value_company(provider.get_company(ticker), today=today)
    except (MarketDataError, InsufficientDataError) as err:
        return None, str(err)
    return ebitda_margin(valued.ltm), None


def _build_filter(
    provider: MarketDataProvider, target: CompanyProfile, today: date, flags: list[DataQualityFlag]
) -> ProxyFilter:
    margin, why = _margin_of(provider, target.ticker, today)
    if why is not None or margin is None:
        detail = why or "LTM revenue or EBITDA is not reported"
        flags.append(
            DataQualityFlag(
                code=FlagCode.PROXY_FILTER_PARTIAL,
                message=(
                    f"{target.ticker}'s EBITDA margin is unavailable ({detail}), so proxy peers were filtered "
                    "on sector only."
                ),
            )
        )
    elif margin <= 0:
        # A relative band around a zero or negative margin admits nothing
        # (or the wrong sign); the test is skipped rather than misapplied.
        flags.append(
            DataQualityFlag(
                code=FlagCode.PROXY_FILTER_PARTIAL,
                message=(
                    f"{target.ticker}'s EBITDA margin is {_pct(margin)}, not positive, so no relative margin band "
                    "can be set; proxy peers were filtered on sector only."
                ),
            )
        )
        margin = None
    if target.sector is None:
        flags.append(
            DataQualityFlag(
                code=FlagCode.PROXY_FILTER_PARTIAL,
                message=f"{target.ticker} has no sector classification, so proxy peers were filtered on EBITDA margin only.",
            )
        )
    return ProxyFilter(
        sector=target.sector,
        ebitda_margin=margin,
        margin_band=MARGIN_BAND,
        margin_low=None if margin is None else margin * (1 - MARGIN_BAND),
        margin_high=None if margin is None else margin * (1 + MARGIN_BAND),
    )


def _judge(
    provider: MarketDataProvider, target: CompanyProfile, peer: ProxyPeer, rule: ProxyFilter, today: date
) -> SuggestedPeer | DroppedProxyPeer:
    """Apply the comparability filter to one proxy name."""

    def dropped(kind: ProxyDropRule, reason: str, profile: CompanyProfile | None = None, margin: float | None = None):
        return DroppedProxyPeer(
            ticker=peer.ticker,
            name=peer.name,
            rule=kind,
            reason=reason,
            sector=profile.sector if profile else None,
            industry=profile.industry if profile else None,
            ebitda_margin=margin,
        )

    try:
        profile = provider.get_profile(peer.ticker)
    except MarketDataError as err:
        return dropped(ProxyDropRule.UNVALUABLE, str(err))
    if not same_currency(target, profile):
        return dropped(
            ProxyDropRule.UNVALUABLE,
            f"trades in {profile.price_currency} and reports in {profile.reporting_currency}; the target in "
            f"{target.price_currency}/{target.reporting_currency}",
            profile,
        )
    if rule.sector is not None and profile.sector != rule.sector:
        where = f"{profile.sector} sector" if profile.sector else "no sector classification"
        if profile.industry:
            where += f" ({profile.industry})"
        return dropped(ProxyDropRule.SECTOR, f"{where}, not {rule.sector}", profile)

    margin: float | None = None
    if rule.ebitda_margin is not None:
        margin, why = _margin_of(provider, peer.ticker, today)
        if why is not None:
            return dropped(ProxyDropRule.UNVALUABLE, why, profile)
        if margin is None:
            return dropped(ProxyDropRule.NO_MARGIN, "LTM EBITDA margin unavailable (revenue or EBITDA not reported)", profile)
        if not rule.margin_low <= margin <= rule.margin_high:
            return dropped(
                ProxyDropRule.MARGIN,
                f"EBITDA margin {_pct(margin)} against the target's {_pct(rule.ebitda_margin)}, outside "
                f"{_pct(rule.margin_low)}–{_pct(rule.margin_high)} (within {rule.margin_band * 100:.0f}% of the target's)",
                profile,
                margin,
            )
    return SuggestedPeer(**profile.model_dump(), sources=[PeerSource.PROXY], ebitda_margin=margin)


def _dropped_flag(target: CompanyProfile, total: int, dropped: list[DroppedProxyPeer]) -> DataQualityFlag:
    counts = {rule: sum(1 for d in dropped if d.rule is rule) for rule in ProxyDropRule}
    names = {
        ProxyDropRule.SECTOR: "outside the sector",
        ProxyDropRule.MARGIN: "EBITDA margin too far",
        ProxyDropRule.NO_MARGIN: "no margin",
        ProxyDropRule.UNVALUABLE: "cannot be valued",
    }
    breakdown = ", ".join(f"{names[r]}: {n}" for r, n in counts.items() if n)
    return DataQualityFlag(
        code=FlagCode.PROXY_PEERS_DROPPED,
        message=(
            f"{len(dropped)} of {_plural(total, 'compensation peer')} in the proxy statement "
            f"{'was' if len(dropped) == 1 else 'were'} filtered out as not business comparables of {target.ticker} "
            f"({breakdown}). Each is listed with its reason and can be added back by hand."
        ),
    )


def _proxy_step(
    source: ProxyPeerSource,
    provider: MarketDataProvider,
    target: CompanyProfile,
    flags: list[DataQualityFlag],
    *,
    today: date,
) -> tuple[ProxyPeerGroup | None, list[SuggestedPeer] | None, list[DroppedProxyPeer], ProxyFilter | None]:
    """Read the proxy peer group, then filter it for comparability.
    Returns (group, kept, dropped, filter); kept is None when the group
    could not be read well enough to filter, and empty when nothing
    survived. Every reason not to contribute is appended to flags."""
    try:
        group = source.proxy_peer_group(target.ticker)
    except ProxyPeersError as err:
        flags.append(_unavailable_flag(err.detail))
        return None, None, [], None
    except Exception as err:  # noqa: BLE001 - parsing prose; a bug here must not fail the suggestion
        flags.append(_unavailable_flag(f"reading the proxy statement failed ({type(err).__name__}: {err})"))
        return None, None, [], None

    if group.confidence is Confidence.LOW:
        names = ", ".join([p.name for p in group.peers] + group.unmatched)
        flags.append(
            DataQualityFlag(
                code=FlagCode.PROXY_PEERS_LOW_CONFIDENCE,
                message=(
                    f"The {group.proxy_year} proxy statement's peer group could not be read reliably: only "
                    f"{_plural(len(group.peers), 'name')} mapped to a ticker ({names}). Candidates come from the "
                    f"industry screen only; check the filing at {group.document_url}."
                ),
            )
        )
        return group, None, [], None

    rule = _build_filter(provider, target, today, flags)
    kept: list[SuggestedPeer] = []
    dropped: list[DroppedProxyPeer] = []
    for peer in group.peers:
        if peer.ticker == target.ticker:
            continue  # a filer is nobody's peer; the SEC list can map a target's own name
        verdict = _judge(provider, target, peer, rule, today)
        (kept if isinstance(verdict, SuggestedPeer) else dropped).append(verdict)

    if group.unmatched:
        flags.append(
            DataQualityFlag(
                code=FlagCode.PROXY_PEERS_UNMATCHED,
                message=(
                    f"{_plural(len(group.unmatched), 'name')} in the proxy peer group could not be mapped to a ticker "
                    f"(private, acquired, or not an SEC registrant); add them by hand if they trade: {', '.join(group.unmatched)}"
                ),
            )
        )
    if dropped:
        flags.append(_dropped_flag(target, len(group.peers), dropped))
    if not kept:
        flags.append(
            _unavailable_flag(
                f"none of the {_plural(len(group.peers), 'compensation peer')} in the {group.proxy_year} proxy "
                f"statement passed the comparability filter (each is listed with its reason and can be added back)"
            )
        )
    return group, kept, dropped, rule


# --- Union ----------------------------------------------------------------


def _combine(proxy_peers: list[SuggestedPeer], screened: list[CompanyProfile]) -> list[SuggestedPeer]:
    """Union of the two sources, strongest signal first: names in both
    (proxy order, carrying the proxy entry's margin), then proxy-only,
    then screen-only (closest in size first, as screened)."""
    screened_tickers = {c.ticker for c in screened}
    from_proxy = {p.ticker for p in proxy_peers}
    both = [
        p.model_copy(update={"sources": [PeerSource.PROXY, PeerSource.SCREEN]})
        for p in proxy_peers
        if p.ticker in screened_tickers
    ]
    proxy_only = [p for p in proxy_peers if p.ticker not in screened_tickers]
    screen_only = [
        SuggestedPeer(**c.model_dump(), sources=[PeerSource.SCREEN]) for c in screened if c.ticker not in from_proxy
    ]
    return both + proxy_only + screen_only


# --- Entry point ----------------------------------------------------------


def suggest_peers(
    provider: MarketDataProvider,
    ticker: str,
    *,
    proxy_source: ProxyPeerSource | None = None,
    today: date | None = None,
    low_multiple: float = MARKET_CAP_LOW_MULTIPLE,
    high_multiple: float = MARKET_CAP_HIGH_MULTIPLE,
    limit: int = DEFAULT_LIMIT,
) -> PeerSuggestions:
    key = normalise_ticker(ticker)
    profile = provider.get_profile(key)
    if profile.market_cap is None or profile.market_cap <= 0:
        raise InsufficientDataError(key, "market cap unavailable; cannot size a peer band")
    if profile.industry is None and profile.sector is None:
        raise InsufficientDataError(key, "sector and industry unknown; nothing to screen on")

    band = market_cap_band(profile.market_cap, low_multiple=low_multiple, high_multiple=high_multiple)
    flags: list[DataQualityFlag] = []
    group: ProxyPeerGroup | None = None
    kept: list[SuggestedPeer] | None = None
    dropped: list[DroppedProxyPeer] = []
    rule: ProxyFilter | None = None
    if proxy_source is not None:
        group, kept, dropped, rule = _proxy_step(proxy_source, provider, profile, flags, today=today or date.today())

    basis = MatchBasis.INDUSTRY if profile.industry is not None else MatchBasis.SECTOR
    if basis is MatchBasis.SECTOR:
        flags.append(_sector_flag(profile))
    screened = _screen(provider, profile, band, basis)

    peers = _combine(kept or [], screened)[:limit]
    if len(peers) < MIN_PEERS:
        flags.append(_thin_flag(profile, band, peers, proxy_ran=kept is not None))
    return PeerSuggestions(
        target=profile,
        market_cap_band=band,
        proxy_label=_proxy_label(profile, group) if kept is not None else None,
        screen_label=_screen_label(profile, band, basis),
        proxy=group,
        proxy_filter=rule,
        proxy_dropped=dropped,
        peers=peers,
        flags=flags,
    )
