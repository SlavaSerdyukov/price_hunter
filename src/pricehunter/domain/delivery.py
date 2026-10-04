"""Destination-scoped evidence, independent of catalog market and item-price alerts."""

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from pricehunter.domain.markets import CountryCode
from pricehunter.domain.money import AncillaryMoney as AncillaryMoney
from pricehunter.domain.money import Money


def normalize_postal(value: object) -> str:
    if not isinstance(value, str) or not value.isprintable():
        raise ValueError("Invalid postal code")
    result = " ".join(unicodedata.normalize("NFC", value).strip().upper().split())
    if not 1 <= len(result) <= 20:
        raise ValueError("Invalid postal code")
    return result


PostalCode = Annotated[str, BeforeValidator(normalize_postal), Field(min_length=1, max_length=20)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DeliveryContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    country: CountryCode
    postal_code: PostalCode | None = Field(default=None, repr=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.country, self.postal_code]).encode()).hexdigest()

    @property
    def country_key(self) -> str:
        return DeliveryContext(country=self.country).fingerprint


class TaxStatus(StrEnum):
    INCLUDED = "included"
    ADDITIONAL = "additional"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class DeliveryScope(StrEnum):
    UNKNOWN = "unknown"
    COUNTRY = "country"
    EXACT = "exact"


class DeliveryStatus(StrEnum):
    UNSUPPORTED = "unsupported"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    FAILED = "failed"
    COMPLETE = "complete"


class DeliveryAvailability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class DeliveryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    country: CountryCode | None = None
    scope: DeliveryScope = DeliveryScope.UNKNOWN
    destination_key: Fingerprint | None = Field(default=None, repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    shipping_price: AncillaryMoney | None = None
    tax_status: TaxStatus = TaxStatus.UNKNOWN
    tax_amount: AncillaryMoney | None = None
    availability: DeliveryAvailability = DeliveryAvailability.UNKNOWN
    quoted_at: AwareDatetime
    expires_at: AwareDatetime
    source: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.:-]+$")
    status: DeliveryStatus = DeliveryStatus.INCOMPLETE

    @model_validator(mode="after")
    def consistent(self) -> "DeliveryEvidence":
        if self.tax_status == TaxStatus.ADDITIONAL and self.tax_amount is None:
            raise ValueError("Additional tax requires an explicit amount")
        if not timedelta(0) < self.expires_at - self.quoted_at <= timedelta(days=1):
            raise ValueError("Quote validity must be positive and at most one day")
        if self.scope != DeliveryScope.UNKNOWN and (not self.country or not self.destination_key):
            raise ValueError("Scoped delivery evidence requires country and destination key")
        if (
            self.scope == DeliveryScope.COUNTRY
            and self.destination_key != DeliveryContext(country=self.country or "BE").fingerprint
        ):
            raise ValueError("Country-wide quote must use country key")
        return self

    def matches(self, context: DeliveryContext) -> bool:
        if self.country != context.country:
            return False
        return (
            self.scope == DeliveryScope.EXACT and self.destination_key == context.fingerprint
        ) or (self.scope == DeliveryScope.COUNTRY and self.destination_key == context.country_key)

    def current(self, now: datetime) -> bool:
        return self.quoted_at <= now < self.expires_at


class DeliveryQuoteData(DeliveryEvidence):
    offer_id: UUID
    snapshot_key: Fingerprint


class DeliveryOfferReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    offer_id: UUID
    snapshot_key: Fingerprint
    external_id: str
    store_slug: str
    market_country: CountryCode
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    price: Money
    url: str


def snapshot_key(price: Decimal, currency: str, external_id: str, url: str) -> str:
    # Decimal scale is storage formatting, not a new price (299 == 299.0000).
    return hashlib.sha256(
        json.dumps([str(price.normalize()), currency, external_id, url]).encode()
    ).hexdigest()


def delivered_total(price: Decimal, currency: str, evidence: DeliveryEvidence) -> Decimal | None:
    """Only native-currency, explicit ancillary costs. Freshness/scope checked by caller."""
    if currency != evidence.currency or evidence.shipping_price is None:
        return None
    if evidence.tax_status == TaxStatus.UNKNOWN:
        return None
    tax = evidence.tax_amount if evidence.tax_status == TaxStatus.ADDITIONAL else Decimal(0)
    if tax is None:
        return None
    return price + evidence.shipping_price + tax


STATIC_DELIVERY_FIELDS = (
    "shipping_price",
    "delivery_country",
    "delivery_scope",
    "delivery_destination_key",
    "delivery_currency",
    "tax_amount",
    "tax_status",
    "delivery_availability",
    "delivery_quoted_at",
    "delivery_expires_at",
)
