from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from pricehunter.domain.products import (
    ProductOfferData,
    model_code,
    normalized,
    trade_ids,
    validate_trade_identifier,
)


@dataclass(frozen=True)
class ProviderCandidate:
    data: ProductOfferData
    provider_rank: int


def query_evidence_rank(data: ProductOfferData, query: str) -> int:
    """Cheap ascending retrieval priority; never canonical identity evidence.

    Prices, commercial metadata and listing/Product IDs are intentionally absent.
    Conflicting/invalid trade identifiers cannot receive an identifier-query boost.
    """
    text = normalized(query)
    if not text:
        return 5
    try:
        query_id = validate_trade_identifier(query).zfill(14)
    except ValueError:
        query_id = None
    if query_id is not None and (identifiers := trade_ids(data)):
        try:
            identifiers = {validate_trade_identifier(value).zfill(14) for value in identifiers}
        except ValueError:
            return 5
        return 0 if identifiers == {query_id} else 5
    code = model_code(query)
    manufacturer_codes = {model_code(value) for value in (data.mpn, data.model) if value}
    manufacturer_codes.discard("")
    if code and code in manufacturer_codes:
        return 1
    brand = model_code(data.brand or "")
    if brand and code in {brand + value for value in manufacturer_codes}:
        return 2
    title = normalized(data.title)
    if text == title:
        return 3
    return 4 if text in title else 5


class ProviderSearchOutcome(BaseModel):
    provider: str
    status: Literal["NETWORK_FAILED", "PARTIAL", "SUCCESS", "EMPTY"] = "EMPTY"
    result_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    errors: list[str] = Field(default_factory=list)
