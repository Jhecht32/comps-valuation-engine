"""Trading multiples with explicit NM suppression.

| Multiple    | Numerator   | Denominator       |
|-------------|-------------|-------------------|
| EV/Revenue  | EV          | LTM Revenue       |
| EV/EBITDA   | EV          | LTM EBITDA        |
| EV/EBIT     | EV          | LTM EBIT          |
| P/E         | Share price | LTM diluted EPS   |

A multiple is NM when its denominator is unavailable, zero, or negative,
when EV/EBITDA exceeds the 100x sanity cap, or (for the EV multiples)
when enterprise value itself is negative. NM is a first-class result
carrying its reason, so the UI can render "NM" and statistics can exclude
it without guessing.
"""

from app.valuation.models import (
    DataQualityFlag,
    DilutedEPS,
    FlagCode,
    LTMFinancials,
    MultipleValue,
    Multiples,
)

EV_EBITDA_NM_CAP = 100.0


def ltm_diluted_eps(
    *,
    net_income: float | None,
    preferred_dividends: float | None,
    nci_income: float | None,
    diluted_shares: float | None,
) -> DilutedEPS:
    """LTM diluted EPS on an income-to-common basis.

    EPS = (LTM net income - preferred dividends - income attributable to
    NCI) / current diluted share count, so P/E measures the same claim on
    earnings that equity value measures on the balance sheet.

    For the breakouts, None means "not broken out by the source" and 0.0
    means known-zero; the provider should pass 0.0 when the balance sheet
    shows no preferred equity or NCI. If the source reports income
    attributable to common directly, pass it as net_income with both
    breakouts set to 0.0. Unknown breakouts fall back to consolidated
    figures with a flag, never silently.

    Uses the current diluted count (consistent with the equity value
    calculation) rather than summing per-quarter EPS, which would be
    distorted by share count changes across the LTM window.
    """
    if net_income is None or diluted_shares is None or diluted_shares <= 0:
        return DilutedEPS(value=None, flags=[])

    income_to_common = net_income
    not_broken_out = []
    if preferred_dividends is None:
        not_broken_out.append("preferred dividends")
    else:
        income_to_common -= preferred_dividends
    if nci_income is None:
        not_broken_out.append("income attributable to NCI")
    else:
        income_to_common -= nci_income

    flags: list[DataQualityFlag] = []
    if not_broken_out:
        flags.append(
            DataQualityFlag(
                code=FlagCode.EPS_FROM_CONSOLIDATED_NI,
                message=f"{' and '.join(not_broken_out)} not broken out by the source; "
                "EPS uses consolidated net income without that deduction.",
            )
        )
    return DilutedEPS(value=income_to_common / diluted_shares, flags=flags)


def _ratio(numerator: float, denominator: float | None, label: str) -> MultipleValue:
    if denominator is None:
        return MultipleValue(value=None, is_nm=True, nm_reason=f"LTM {label} unavailable")
    if denominator <= 0:
        return MultipleValue(value=None, is_nm=True, nm_reason=f"LTM {label} is negative or zero")
    return MultipleValue(value=numerator / denominator, is_nm=False)


def compute_multiples(
    *,
    enterprise_value: float,
    share_price: float,
    ltm: LTMFinancials,
    diluted_eps: float | None,
) -> Multiples:
    if enterprise_value < 0:
        # Mathematically defined but economically meaningless; letting a
        # negative multiple into the peer set would corrupt the median.
        negative_ev = MultipleValue(
            value=None,
            is_nm=True,
            nm_reason="Enterprise value is negative; EV multiples are not meaningful",
        )
        return Multiples(
            ev_revenue=negative_ev,
            ev_ebitda=negative_ev,
            ev_ebit=negative_ev,
            pe=_ratio(share_price, diluted_eps, "diluted EPS"),
        )

    ev_ebitda = _ratio(enterprise_value, ltm.ebitda, "EBITDA")
    if not ev_ebitda.is_nm and ev_ebitda.value > EV_EBITDA_NM_CAP:
        ev_ebitda = MultipleValue(
            value=None,
            is_nm=True,
            nm_reason=f"EV/EBITDA of {ev_ebitda.value:.1f}x exceeds the {EV_EBITDA_NM_CAP:.0f}x cap",
        )

    return Multiples(
        ev_revenue=_ratio(enterprise_value, ltm.revenue, "revenue"),
        ev_ebitda=ev_ebitda,
        ev_ebit=_ratio(enterprise_value, ltm.ebit, "EBIT"),
        pe=_ratio(share_price, diluted_eps, "diluted EPS"),
    )
