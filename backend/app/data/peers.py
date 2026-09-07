"""Peer selection rule, applied identically to every source.

A suggested peer shares the target's industry (or, on the sector basis,
its sector), sits inside a market-cap band of 0.33x-3.0x the target, and
trades and reports in the target's currencies. The band is a
size-similarity heuristic, not a valuation input: a company a quarter
the size of the target is usually at a different stage of its life and
priced accordingly, and one four times larger dominates the median.
"""

import math
from enum import Enum
from typing import Iterable

from pydantic import BaseModel

from app.data.provider import CompanyProfile

MARKET_CAP_LOW_MULTIPLE = 0.33
MARKET_CAP_HIGH_MULTIPLE = 3.0


class MatchBasis(str, Enum):
    INDUSTRY = "industry"
    SECTOR = "sector"


class MarketCapBand(BaseModel):
    low: float
    high: float
    low_multiple: float
    high_multiple: float


def market_cap_band(
    target_market_cap: float | None,
    *,
    low_multiple: float = MARKET_CAP_LOW_MULTIPLE,
    high_multiple: float = MARKET_CAP_HIGH_MULTIPLE,
) -> MarketCapBand:
    if target_market_cap is None or target_market_cap <= 0:
        raise ValueError(f"target market cap must be positive, got {target_market_cap!r}")
    return MarketCapBand(
        low=target_market_cap * low_multiple,
        high=target_market_cap * high_multiple,
        low_multiple=low_multiple,
        high_multiple=high_multiple,
    )


def _same_currency(target: CompanyProfile, candidate: CompanyProfile) -> bool:
    # When the target's currency is known, a candidate must match it; an
    # unknown candidate currency cannot be confirmed and is dropped, the
    # same stance the engine's currency guard takes.
    for attr in ("price_currency", "reporting_currency"):
        want = getattr(target, attr)
        if want is not None and getattr(candidate, attr) != want:
            return False
    return True


def _company_key(profile: CompanyProfile) -> str | None:
    return profile.name.strip().casefold() if profile.name else None


def _collapse_share_classes(
    ordered: list[CompanyProfile], *, target: CompanyProfile
) -> list[CompanyProfile]:
    """One row per company: dual listings (LEN / LEN-B, GOOGL / GOOG)
    share a name, and two rows would double-count one business in the
    median. The input is cap-descending, so the larger listing is kept.
    The target's own other share class is a peer of nobody."""
    seen: set[str] = set()
    target_key = _company_key(target)
    if target_key is not None:
        seen.add(target_key)
    kept: list[CompanyProfile] = []
    for c in ordered:
        key = _company_key(c)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        kept.append(c)
    return kept


def _size_distance(candidate: CompanyProfile, target: CompanyProfile) -> float:
    """How far a candidate's market cap sits from the target's, symmetric
    in ratio terms: half the size and twice the size are equally far."""
    return abs(math.log(candidate.market_cap / target.market_cap))


def select_peers(
    target: CompanyProfile,
    candidates: Iterable[CompanyProfile],
    *,
    basis: MatchBasis,
    low_multiple: float = MARKET_CAP_LOW_MULTIPLE,
    high_multiple: float = MARKET_CAP_HIGH_MULTIPLE,
) -> list[CompanyProfile]:
    """Filter candidates to those that qualify as peers of the target,
    one entry per company, ordered by similarity of size to the target
    (closest market cap first)."""
    band = market_cap_band(target.market_cap, low_multiple=low_multiple, high_multiple=high_multiple)
    attr = "industry" if basis is MatchBasis.INDUSTRY else "sector"
    want = getattr(target, attr)
    if want is None:
        return []

    kept: dict[str, CompanyProfile] = {}
    for c in candidates:
        if c.ticker == target.ticker or c.ticker in kept:
            continue
        if getattr(c, attr) != want:
            continue
        if c.market_cap is None or not band.low <= c.market_cap <= band.high:
            continue
        if not _same_currency(target, c):
            continue
        kept[c.ticker] = c
    by_cap = sorted(kept.values(), key=lambda c: c.market_cap, reverse=True)
    companies = _collapse_share_classes(by_cap, target=target)
    return sorted(companies, key=lambda c: _size_distance(c, target))
