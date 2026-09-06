"""Implied valuation of the target: the EV bridge run in reverse.

    Implied EV        = peer multiple x target LTM metric
    Implied Equity    = Implied EV - total debt - preferred - NCI
                        + cash + short-term investments
    Implied Per Share = Implied Equity / target diluted shares

P/E skips the bridge: Implied Per Share = peer P/E x target LTM diluted EPS.

Short-term investments are added back alongside cash so the reverse
bridge is the exact inverse of the forward bridge: applying a company's
own multiple to itself must recover its own share price.
"""

from app.valuation.models import (
    EVBridge,
    ImpliedRange,
    LTMFinancials,
    MidBasis,
    MultipleKind,
    MultipleStats,
)

_EV_METRIC_FIELDS = {
    MultipleKind.EV_REVENUE: "revenue",
    MultipleKind.EV_EBITDA: "ebitda",
    MultipleKind.EV_EBIT: "ebit",
}


def _target_metric(
    kind: MultipleKind, ltm: LTMFinancials, diluted_eps: float | None
) -> tuple[float | None, str]:
    if kind is MultipleKind.PE:
        return diluted_eps, "diluted EPS"
    field = _EV_METRIC_FIELDS[kind]
    return getattr(ltm, field), field


def implied_per_share(
    *,
    kind: MultipleKind,
    multiple: float,
    bridge: EVBridge,
    ltm: LTMFinancials,
    diluted_eps: float | None = None,
) -> float:
    metric, label = _target_metric(kind, ltm, diluted_eps)
    if metric is None:
        raise ValueError(f"Target LTM {label} unavailable")

    if kind is MultipleKind.PE:
        return multiple * metric

    implied_ev = multiple * metric
    implied_equity = (
        implied_ev
        - bridge.total_debt
        - bridge.preferred_equity
        - bridge.noncontrolling_interest
        + bridge.cash_and_equivalents
        + bridge.short_term_investments
    )
    return implied_equity / bridge.diluted_shares


def implied_range(
    *,
    kind: MultipleKind,
    stats: MultipleStats,
    bridge: EVBridge,
    ltm: LTMFinancials,
    diluted_eps: float | None = None,
    mid_basis: MidBasis = MidBasis.MEDIAN,
) -> ImpliedRange:
    def nm(reason: str) -> ImpliedRange:
        return ImpliedRange(
            kind=kind, low=None, mid=None, high=None, n=stats.n, is_nm=True, nm_reason=reason
        )

    if stats.n == 0:
        return nm("No peer companies with a meaningful multiple")

    metric, label = _target_metric(kind, ltm, diluted_eps)
    if metric is None:
        return nm(f"Target LTM {label} unavailable")
    if metric <= 0:
        return nm(f"Target LTM {label} is negative or zero; multiple cannot be applied")

    mid_multiple = stats.median if mid_basis is MidBasis.MEDIAN else stats.mean

    def per_share(multiple: float) -> float:
        return implied_per_share(
            kind=kind, multiple=multiple, bridge=bridge, ltm=ltm, diluted_eps=diluted_eps
        )

    return ImpliedRange(
        kind=kind,
        low=per_share(stats.p25),
        mid=per_share(mid_multiple),
        high=per_share(stats.p75),
        n=stats.n,
        is_nm=False,
    )
