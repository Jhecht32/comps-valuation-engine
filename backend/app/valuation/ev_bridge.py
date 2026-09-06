"""Enterprise value bridge.

    Equity Value      = diluted shares x current share price
    Enterprise Value  = Equity Value
                      + total debt
                      + preferred equity
                      + noncontrolling interest
                      - cash and cash equivalents
                      - short-term investments / marketable securities

Diluted shares are required; basic shares are an explicit, flagged
fallback, never a silent substitution.
"""

from app.valuation.models import DataQualityFlag, EVBridge, FlagCode


def build_ev_bridge(
    *,
    share_price: float,
    diluted_shares: float | None = None,
    basic_shares: float | None = None,
    total_debt: float | None = None,
    preferred_equity: float | None = None,
    noncontrolling_interest: float | None = None,
    cash_and_equivalents: float | None = None,
    short_term_investments: float | None = None,
) -> EVBridge:
    if share_price is None or share_price <= 0:
        raise ValueError(f"share_price must be positive, got {share_price!r}")

    flags: list[DataQualityFlag] = []

    shares_are_basic_fallback = False
    if diluted_shares is not None and diluted_shares > 0:
        shares = float(diluted_shares)
    elif basic_shares is not None and basic_shares > 0:
        shares = float(basic_shares)
        shares_are_basic_fallback = True
        flags.append(
            DataQualityFlag(
                code=FlagCode.BASIC_SHARES_FALLBACK,
                message="Diluted share count unavailable; basic shares used as an approximation.",
            )
        )
    else:
        raise ValueError("No usable share count: both diluted and basic shares are unavailable")

    # Debt and cash lines exist for every real company, so a missing value
    # is a data problem worth flagging. Preferred, NCI, and short-term
    # investments are genuinely absent for most companies; treat absence
    # as zero without a flag.
    if total_debt is None:
        total_debt = 0.0
        flags.append(
            DataQualityFlag(
                code=FlagCode.MISSING_TOTAL_DEBT,
                message="Total debt unavailable; treated as zero in the EV bridge.",
            )
        )
    if cash_and_equivalents is None:
        cash_and_equivalents = 0.0
        flags.append(
            DataQualityFlag(
                code=FlagCode.MISSING_CASH,
                message="Cash and equivalents unavailable; treated as zero in the EV bridge.",
            )
        )
    preferred_equity_reported = preferred_equity is not None
    noncontrolling_interest_reported = noncontrolling_interest is not None
    short_term_investments_reported = short_term_investments is not None
    preferred_equity = preferred_equity if preferred_equity is not None else 0.0
    noncontrolling_interest = (
        noncontrolling_interest if noncontrolling_interest is not None else 0.0
    )
    short_term_investments = (
        short_term_investments if short_term_investments is not None else 0.0
    )

    equity_value = shares * share_price
    enterprise_value = (
        equity_value
        + total_debt
        + preferred_equity
        + noncontrolling_interest
        - cash_and_equivalents
        - short_term_investments
    )

    return EVBridge(
        share_price=share_price,
        diluted_shares=shares,
        shares_are_basic_fallback=shares_are_basic_fallback,
        equity_value=equity_value,
        total_debt=total_debt,
        preferred_equity=preferred_equity,
        preferred_equity_reported=preferred_equity_reported,
        noncontrolling_interest=noncontrolling_interest,
        noncontrolling_interest_reported=noncontrolling_interest_reported,
        cash_and_equivalents=cash_and_equivalents,
        short_term_investments=short_term_investments,
        short_term_investments_reported=short_term_investments_reported,
        enterprise_value=enterprise_value,
        flags=flags,
    )
