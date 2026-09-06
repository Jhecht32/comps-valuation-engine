"""Distribution statistics for peer multiples.

Percentiles use linear interpolation between closest ranks — the same
method as Excel's PERCENTILE.INC and numpy's default — so output ties out
against a banker's spreadsheet.
"""

from typing import Iterable, Sequence

from app.valuation.models import MultipleStats, MultipleValue


def percentile_inc(values: Sequence[float], p: float) -> float:
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    ordered = sorted(values)
    rank = p * (len(ordered) - 1)
    lower = int(rank)
    fraction = rank - lower
    if fraction == 0.0:
        return ordered[lower]
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


def summarize_multiples(multiples: Iterable[MultipleValue]) -> MultipleStats:
    values = [m.value for m in multiples if not m.is_nm and m.value is not None]
    if not values:
        return MultipleStats(n=0, min=None, p25=None, median=None, mean=None, p75=None, max=None)
    return MultipleStats(
        n=len(values),
        min=min(values),
        p25=percentile_inc(values, 0.25),
        median=percentile_inc(values, 0.50),
        mean=sum(values) / len(values),
        p75=percentile_inc(values, 0.75),
        max=max(values),
    )
