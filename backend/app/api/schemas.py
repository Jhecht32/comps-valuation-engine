"""Request and response schemas for the HTTP API.

Responses are the service-layer result models re-exported under API
names so the OpenAPI document reads naturally; requests are defined
here. Tickers are normalised (stripped, upper-cased) at the boundary.
"""

from typing import Annotated

from fastapi import Path
from pydantic import BaseModel, Field, StringConstraints

from app.services.models import (
    CompanyValuation,
    CompsResult,
    PeerSuggestions,
    TickerError,
)
from app.valuation.models import MidBasis

# Exchange tickers plus the separators Yahoo uses for share classes and
# foreign listings (BRK-B, RDS.A, 7203.T).
TICKER_PATTERN = r"^[A-Za-z0-9.\-]{1,12}$"

Ticker = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, pattern=TICKER_PATTERN),
]

TickerPath = Annotated[str, Path(pattern=TICKER_PATTERN, description="Exchange ticker, case-insensitive")]

MAX_PEERS = 30


class CompsRequest(BaseModel):
    target: Ticker
    peers: list[Ticker] = Field(min_length=1, max_length=MAX_PEERS)
    mid_basis: MidBasis = MidBasis.MEDIAN


class HealthResponse(BaseModel):
    status: str


CompanyResponse = CompanyValuation
CompsResponse = CompsResult
PeersResponse = PeerSuggestions
ErrorResponse = TickerError
