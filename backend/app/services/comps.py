"""Composition of provider output into engine calls.

value_company is the single path from a CompanySnapshot to a valued
company; run_comps applies it to a target and a peer set and hands the
peer multiples to the statistics and implied-range functions. Nothing
here computes a number: every figure comes back from app.valuation.

LTM is built from the four most recent quarters. The engine's
annual-plus-stub fallback needs YTD stub figures, which a snapshot does
not carry (it carries reported quarters), so a company with fewer than
four quarters is reported as insufficient data rather than stitched
here.
"""

from datetime import date

from app.data.provider import (
    MarketDataError,
    MarketDataProvider,
    reject_currency_mismatches,
)
from app.services.errors import InsufficientDataError, ticker_error
from app.services.models import (
    CompanyValuation,
    CompsResult,
    CompsStatistics,
    ImpliedValuation,
    TickerError,
)
from app.valuation import (
    CompanySnapshot,
    DataQualityFlag,
    MidBasis,
    MultipleKind,
    build_ev_bridge,
    compute_multiples,
    implied_range,
    ltm_diluted_eps,
    ltm_from_quarters,
    summarize_multiples,
)

def normalise_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def _merge_flags(*groups: list[DataQualityFlag]) -> list[DataQualityFlag]:
    seen: set[tuple[str, str]] = set()
    merged: list[DataQualityFlag] = []
    for group in groups:
        for flag in group:
            key = (flag.code, flag.message)
            if key not in seen:
                seen.add(key)
                merged.append(flag)
    return merged


def value_company(snapshot: CompanySnapshot, *, today: date) -> CompanyValuation:
    """Run one snapshot through LTM stitching, the EV bridge, EPS, and
    multiples. The engine's refusals (too few quarters, no share count,
    non-positive price) come back as InsufficientDataError."""
    try:
        ltm = ltm_from_quarters(snapshot.quarters, today=today)
    except ValueError as exc:
        raise InsufficientDataError(snapshot.ticker, str(exc)) from exc

    bs = snapshot.balance_sheet
    try:
        bridge = build_ev_bridge(
            share_price=snapshot.share_price,
            diluted_shares=snapshot.diluted_shares,
            basic_shares=snapshot.basic_shares,
            total_debt=bs.total_debt,
            preferred_equity=bs.preferred_equity,
            noncontrolling_interest=bs.noncontrolling_interest,
            cash_and_equivalents=bs.cash_and_equivalents,
            short_term_investments=bs.short_term_investments,
        )
    except ValueError as exc:
        raise InsufficientDataError(snapshot.ticker, str(exc)) from exc

    eps = ltm_diluted_eps(
        net_income=ltm.net_income,
        preferred_dividends=ltm.preferred_dividends,
        nci_income=ltm.nci_income,
        diluted_shares=bridge.diluted_shares,
    )
    multiples = compute_multiples(
        enterprise_value=bridge.enterprise_value,
        share_price=snapshot.share_price,
        ltm=ltm,
        diluted_eps=eps.value,
    )
    return CompanyValuation(
        ticker=snapshot.ticker,
        name=snapshot.name,
        source=snapshot.source,
        price_currency=snapshot.price_currency,
        reporting_currency=snapshot.reporting_currency,
        share_price=snapshot.share_price,
        price_as_of=snapshot.price_as_of,
        shares_as_of=snapshot.shares_as_of,
        balance_sheet_as_of=bs.as_of,
        bridge=bridge,
        ltm=ltm,
        diluted_eps=eps.value,
        multiples=multiples,
        flags=_merge_flags(bridge.flags, ltm.flags, eps.flags),
    )


def _peer_keys(target_key: str, peers: list[str]) -> list[str]:
    keys: list[str] = []
    for peer in peers:
        key = normalise_ticker(peer)
        if key and key != target_key and key not in keys:
            keys.append(key)
    return keys


def run_comps(
    provider: MarketDataProvider,
    *,
    target: str,
    peers: list[str],
    mid_basis: MidBasis = MidBasis.MEDIAN,
    today: date | None = None,
) -> CompsResult:
    """Value the target and every peer, summarise the peer multiples, and
    apply them back to the target.

    Target failures raise (there is no comps run without a target). Peer
    failures of every kind land in the result's errors map: source
    errors and unknown tickers from get_companies, currency mismatches
    from the guard, and engine refusals from value_company.
    """
    as_of = today if today is not None else date.today()
    target_key = normalise_ticker(target)
    peer_keys = _peer_keys(target_key, peers)

    target_snapshot = provider.get_company(target_key)
    target_valuation = value_company(target_snapshot, today=as_of)

    fetched = provider.get_companies(peer_keys) if peer_keys else {}
    fetched = reject_currency_mismatches(target_snapshot, fetched)

    valued: list[CompanyValuation] = []
    errors: dict[str, TickerError] = {}
    for key in peer_keys:
        item = fetched.get(key)
        if item is None:
            item = MarketDataError(f"{key}: provider returned no result")
        if isinstance(item, MarketDataError):
            errors[key] = ticker_error(key, item)
            continue
        try:
            valued.append(value_company(item, today=as_of))
        except InsufficientDataError as exc:
            errors[key] = ticker_error(key, exc)

    statistics = CompsStatistics(
        **{
            kind.value: summarize_multiples(getattr(p.multiples, kind.value) for p in valued)
            for kind in MultipleKind
        }
    )
    implied = ImpliedValuation(
        mid_basis=mid_basis,
        **{
            kind.value: implied_range(
                kind=kind,
                stats=getattr(statistics, kind.value),
                bridge=target_valuation.bridge,
                ltm=target_valuation.ltm,
                diluted_eps=target_valuation.diluted_eps,
                mid_basis=mid_basis,
            )
            for kind in MultipleKind
        },
    )
    return CompsResult(
        as_of=as_of,
        target=target_valuation,
        peers=valued,
        statistics=statistics,
        implied=implied,
        errors=errors,
    )
