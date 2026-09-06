"""LTM (last twelve months) stitching.

Primary path:   LTM = sum of the four most recent reported quarters.
Fallback path:  LTM = FY + current YTD stub - prior-year comparable stub.

EBITDA is derived as LTM EBIT + LTM D&A, with D&A taken from the cash
flow statement. Every approximation or data gap is recorded as a flag on
the result; nothing is silently substituted.
"""

from datetime import date, timedelta
from typing import Sequence

from app.valuation.models import (
    DataQualityFlag,
    FlagCode,
    LTMFinancials,
    LTMMethod,
    PeriodFinancials,
    QuarterlyFinancials,
)

# Measured as (newest period_end - oldest period_end) across the four
# selected quarters — NOT fiscal coverage from the earliest quarter's
# start. Four consecutive quarter-ends span ~273 days on this measure;
# one missing quarter stretches it to ~365, so 320 separates the two.
_MAX_CONSECUTIVE_SPAN_DAYS = 320

# Quarterly filers report within ~45 days of period end. If the latest
# period end is older than a quarter plus that grace period, at least one
# newer quarter should exist and the LTM window is stale.
_STALE_AFTER_DAYS = 135

_METRIC_FIELDS = ("revenue", "gross_profit", "ebit", "net_income", "d_and_a")


def _missing_flag(field: str, detail: str) -> DataQualityFlag:
    if field == "d_and_a":
        return DataQualityFlag(
            code=FlagCode.MISSING_DA,
            message=f"D&A unavailable {detail}; LTM EBITDA cannot be computed.",
        )
    return DataQualityFlag(
        code=FlagCode.MISSING_LINE_ITEM,
        message=f"{field} unavailable {detail}; LTM {field} suppressed.",
    )


def _staleness_flag(as_of: date, today: date | None) -> DataQualityFlag | None:
    if today is not None and today - as_of > timedelta(days=_STALE_AFTER_DAYS):
        return DataQualityFlag(
            code=FlagCode.STALE_FILING,
            message=f"Latest reported period ended {as_of.isoformat()}, more than "
            f"{_STALE_AFTER_DAYS} days ago; a newer filing is likely available.",
        )
    return None


def _assemble(
    metrics: dict[str, float | None],
    *,
    as_of: date,
    method: LTMMethod,
    flags: list[DataQualityFlag],
    today: date | None,
) -> LTMFinancials:
    ebit = metrics["ebit"]
    d_and_a = metrics["d_and_a"]
    ebitda = ebit + d_and_a if ebit is not None and d_and_a is not None else None

    stale = _staleness_flag(as_of, today)
    if stale is not None:
        flags.append(stale)

    return LTMFinancials(
        revenue=metrics["revenue"],
        gross_profit=metrics["gross_profit"],
        ebitda=ebitda,
        ebit=ebit,
        net_income=metrics["net_income"],
        as_of=as_of,
        method=method,
        flags=flags,
    )


def ltm_from_quarters(
    quarters: Sequence[QuarterlyFinancials],
    *,
    today: date | None = None,
) -> LTMFinancials:
    if len(quarters) < 4:
        raise ValueError(
            f"LTM from quarters requires at least 4 reported quarters, got {len(quarters)}"
        )

    recent = sorted(quarters, key=lambda q: q.period_end, reverse=True)[:4]
    flags: list[DataQualityFlag] = []

    span = recent[0].period_end - recent[3].period_end
    if span.days > _MAX_CONSECUTIVE_SPAN_DAYS:
        flags.append(
            DataQualityFlag(
                code=FlagCode.NONCONSECUTIVE_QUARTERS,
                message=f"The four most recent quarters span {span.days} days; a quarter "
                "appears to be missing from the LTM window.",
            )
        )

    metrics: dict[str, float | None] = {}
    for field in _METRIC_FIELDS:
        values = [getattr(q, field) for q in recent]
        if any(v is None for v in values):
            metrics[field] = None
            flags.append(_missing_flag(field, "in at least one quarter"))
        else:
            metrics[field] = sum(values)

    return _assemble(
        metrics,
        as_of=recent[0].period_end,
        method=LTMMethod.QUARTERLY,
        flags=flags,
        today=today,
    )


def ltm_from_annual_and_stub(
    *,
    fy: PeriodFinancials,
    ytd_current: QuarterlyFinancials,
    ytd_prior: PeriodFinancials,
    today: date | None = None,
) -> LTMFinancials:
    flags: list[DataQualityFlag] = [
        DataQualityFlag(
            code=FlagCode.LTM_FROM_ANNUAL_STUB,
            message="LTM built from fiscal-year figures plus YTD stubs rather than "
            "four reported quarters.",
        )
    ]

    metrics: dict[str, float | None] = {}
    for field in _METRIC_FIELDS:
        fy_v = getattr(fy, field)
        cur_v = getattr(ytd_current, field)
        prior_v = getattr(ytd_prior, field)
        if fy_v is None or cur_v is None or prior_v is None:
            metrics[field] = None
            flags.append(_missing_flag(field, "in the FY or stub periods"))
        else:
            metrics[field] = fy_v + cur_v - prior_v

    return _assemble(
        metrics,
        as_of=ytd_current.period_end,
        method=LTMMethod.ANNUAL_STUB,
        flags=flags,
        today=today,
    )
