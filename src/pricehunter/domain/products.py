import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, field_validator


def decimal_input(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("Money must arrive as a decimal string or Decimal")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(decimal_input),
    Field(gt=0, max_digits=18, decimal_places=4, allow_inf_nan=False),
]


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


def validate_trade_identifier(value: str, lengths: tuple[int, ...] = (8, 12, 13, 14)) -> str:
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) not in lengths:
        raise ValueError("Invalid global trade identifier")
    check = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(value[-2::-1]))
    if (10 - check % 10) % 10 != int(value[-1]):
        raise ValueError("Invalid identifier checksum")
    return value


class ProductOfferData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(min_length=1, max_length=50)
    store_slug: str = Field(min_length=1, max_length=80)
    store_name: str = Field(min_length=1, max_length=100)
    store_domain: str = Field(min_length=1, max_length=200)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    external_id: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    price: Money
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    availability: Availability = Availability.UNKNOWN
    original_price: Money | None = None
    image_url: str | None = Field(default=None, max_length=2048)
    brand: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    gtin: str | None = None
    ean: str | None = None
    upc: str | None = None
    asin: str | None = Field(default=None, max_length=20)
    sku: str | None = Field(default=None, max_length=200)
    seller: str | None = Field(default=None, max_length=200)
    variant: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        result = " ".join(value.split())
        if not result:
            raise ValueError("Empty title")
        return result

    @field_validator("gtin", "ean", "upc")
    @classmethod
    def clean_identifier(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        lengths = {"gtin": (8, 12, 13, 14), "ean": (8, 13), "upc": (12,)}
        return validate_trade_identifier(value, lengths[info.field_name or "gtin"])


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def trade_id(offer: ProductOfferData) -> str | None:
    value = offer.gtin or offer.ean or offer.upc
    return value.zfill(14) if value else None


def identity_key(offer: ProductOfferData) -> str:
    identifier = trade_id(offer)
    identity = f"gtin:{identifier}" if identifier else f"{offer.store_slug}:{offer.external_id}"
    variants = {k.casefold(): normalized(v) for k, v in offer.variant.items()}
    raw = json.dumps([identity, variants], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    confidence: Decimal
    method: str


class ProductMatcher:
    def match(self, left: ProductOfferData, right: ProductOfferData) -> MatchResult:
        variants_left = {k.casefold(): normalized(v) for k, v in left.variant.items()}
        variants_right = {k.casefold(): normalized(v) for k, v in right.variant.items()}
        if variants_left != variants_right:
            return MatchResult(False, Decimal("0"), "variant_mismatch")
        a, b = trade_id(left), trade_id(right)
        if a and b:
            return MatchResult(a == b, Decimal("1") if a == b else Decimal("0"), "gtin")
        if left.store_slug == right.store_slug and left.external_id == right.external_id:
            return MatchResult(True, Decimal("1"), "listing")
        if left.brand and right.brand and left.model and right.model:
            matches = (normalized(left.brand), normalized(left.model)) == (
                normalized(right.brand),
                normalized(right.model),
            )
            if matches:
                return MatchResult(True, Decimal("0.90"), "brand_model")
        # Title similarity is a suggestion, never enough to merge catalog records.
        return MatchResult(False, Decimal("0"), "insufficient_evidence")
