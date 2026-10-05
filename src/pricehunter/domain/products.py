import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from pricehunter.core.security import validate_url
from pricehunter.domain.delivery import (
    STATIC_DELIVERY_FIELDS,
    AncillaryMoney,
    DeliveryAvailability,
    DeliveryContext,
    DeliveryEvidence,
    DeliveryScope,
    Fingerprint,
    TaxStatus,
    delivered_total,
)
from pricehunter.domain.markets import CountryCode
from pricehunter.domain.money import Money as Money


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
    country: CountryCode
    source_updated_at: AwareDatetime | None = None
    external_id: str = Field(min_length=1, max_length=200)
    external_merchant_id: str | None = Field(default=None, max_length=100)
    merchant_program_id: UUID | None = None
    feed_generation: int = Field(default=0, ge=0)
    url: str = Field(default="", max_length=2048)
    direct_url: str | None = Field(default=None, max_length=2048)
    affiliate_url: str | None = Field(default=None, max_length=2048)
    affiliate_network: str | None = Field(default=None, max_length=80)
    affiliate_metadata: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(min_length=1, max_length=500)
    price: Money
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    availability: Availability = Availability.UNKNOWN
    shipping_price: AncillaryMoney | None = None
    delivery_country: CountryCode | None = None
    delivery_scope: DeliveryScope = DeliveryScope.UNKNOWN
    delivery_destination_key: Fingerprint | None = None
    delivery_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    tax_amount: AncillaryMoney | None = None
    tax_status: TaxStatus = TaxStatus.UNKNOWN
    delivery_availability: DeliveryAvailability = DeliveryAvailability.UNKNOWN
    delivery_quoted_at: AwareDatetime | None = None
    delivery_expires_at: AwareDatetime | None = None
    original_price: Money | None = None
    image_url: str | None = Field(default=None, max_length=2048)
    brand: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    mpn: str | None = Field(default=None, max_length=200)
    gtin: str | None = None
    ean: str | None = None
    upc: str | None = None
    asin: str | None = Field(default=None, max_length=20)
    sku: str | None = Field(default=None, max_length=200)
    seller: str | None = Field(default=None, max_length=200)
    variant: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def legacy_locator(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "direct_url" not in data and "affiliate_url" not in data:
                data["direct_url"] = data.get("url")
            if not data.get("url"):
                data["url"] = data.get("direct_url") or data.get("affiliate_url") or ""
        return data

    @model_validator(mode="after")
    def safe_destinations(self) -> "ProductOfferData":
        if not (self.direct_url or self.affiliate_url):
            raise ValueError("Offer requires an HTTPS destination")
        for value in (self.url, self.direct_url, self.affiliate_url):
            if value:
                validate_url(value, {urlsplit(value).hostname or ""})
        if self.affiliate_url and not self.affiliate_network:
            raise ValueError("Affiliate URL requires network identity")
        if len(json.dumps(self.affiliate_metadata)) > 4096:
            raise ValueError("Affiliate metadata exceeds budget")
        return self

    @model_validator(mode="after")
    def delivery_data(self) -> "ProductOfferData":
        if self.tax_status == TaxStatus.ADDITIONAL and self.tax_amount is None:
            raise ValueError("Additional tax requires an explicit amount")
        if self.delivery_currency is not None and self.delivery_currency != self.currency:
            raise ValueError("Ancillary costs must use item currency")
        if self.delivery_scope != DeliveryScope.UNKNOWN and self.delivery_country is None:
            raise ValueError("Scoped delivery evidence needs a country")
        if self.delivery_scope == DeliveryScope.EXACT and self.delivery_destination_key is None:
            raise ValueError("Exact delivery evidence needs a destination key")
        if self.delivery_scope == DeliveryScope.COUNTRY:
            key = DeliveryContext(country=self.delivery_country or "BE").fingerprint
            if self.delivery_destination_key not in (None, key):
                raise ValueError("Invalid country delivery key")
            object.__setattr__(self, "delivery_destination_key", key)
        if self.shipping_price is not None or self.tax_amount is not None:
            object.__setattr__(self, "delivery_currency", self.delivery_currency or self.currency)
        if self.delivery_expires_at is not None and (
            self.delivery_quoted_at is None
            or not 0 < (self.delivery_expires_at - self.delivery_quoted_at).total_seconds() <= 86400
        ):
            raise ValueError("Invalid delivery validity interval")
        return self

    def delivery_values(self) -> dict[str, object]:
        values: dict[str, object] = {
            field: getattr(self, field) for field in STATIC_DELIVERY_FIELDS
        }
        values.update(delivery_item_price=self.price, delivery_total=None)
        if self.delivery_quoted_at is not None and self.delivery_expires_at is not None:
            evidence = DeliveryEvidence(
                country=self.delivery_country,
                scope=self.delivery_scope,
                destination_key=self.delivery_destination_key,
                currency=self.delivery_currency or self.currency,
                shipping_price=self.shipping_price,
                tax_status=self.tax_status,
                tax_amount=self.tax_amount,
                availability=self.delivery_availability,
                quoted_at=self.delivery_quoted_at,
                expires_at=self.delivery_expires_at,
                source=self.provider,
            )
            values["delivery_total"] = delivered_total(self.price, self.currency, evidence)
        return values

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
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip().casefold())


def model_code(value: str) -> str:
    # Common formatting separators only: '+' and '/' can distinguish actual models.
    return re.sub(r"[\s._\-‐‑–—]+", "", normalized(value))


VARIANT_KEYS = {
    "colour": "color",
    "couleur": "color",
    "farbe": "color",
    "colore": "color",
    "kolor": "color",
    "цвет": "color",
    "taille": "size",
    "grösse": "size",
    "groesse": "size",
    "talla": "size",
    "taglia": "size",
    "rozmiar": "size",
    "размер": "size",
    "storage": "capacity",
    "storage capacity": "capacity",
    "capacité": "capacity",
    "kapazität": "capacity",
    "capacidad": "capacity",
    "capacità": "capacity",
    "pojemność": "capacity",
    "ёмкость": "capacity",
}
COLORS = {
    "noir": "black",
    "schwarz": "black",
    "negro": "black",
    "nero": "black",
    "czarny": "black",
    "чёрный": "black",
    "черный": "black",
    "blanc": "white",
    "weiß": "white",
    "weiss": "white",
    "blanco": "white",
    "bianco": "white",
    "biały": "white",
    "белый": "white",
    "rouge": "red",
    "rot": "red",
    "rojo": "red",
    "rosso": "red",
    "czerwony": "red",
}


def normalized_variant(variant: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in sorted(variant.items()):
        key = normalized(raw_key).replace("_", " ").replace("-", " ")
        key = VARIANT_KEYS.get(key, normalized(raw_key))
        value = normalized(raw_value)
        if key == "color":
            value = COLORS.get(value, value)
        elif key == "capacity":
            value = re.sub(r"\s+", "", value)
        elif key == "size":
            value = value.replace(",", ".")
        if key in result and result[key] != value:
            # Keep contradictory aliases distinct instead of dropping information.
            result[f"conflict:{normalized(raw_key)}"] = value
        else:
            result[key] = value
    return result


def trade_ids(offer: ProductOfferData) -> set[str]:
    return {value.zfill(14) for value in (offer.gtin, offer.ean, offer.upc) if value}


def trade_id(offer: ProductOfferData) -> str | None:
    values = trade_ids(offer)
    return next(iter(values)) if len(values) == 1 else None


def identity_signals(offer: ProductOfferData) -> list[tuple[str, str]]:
    signals = [("gtin", value) for value in sorted(trade_ids(offer))]
    brand = model_code(offer.brand or "")
    if brand:
        for value in (offer.mpn, offer.model):
            if value and len(model_code(value)) >= 3:
                signals.append(("brand_model", f"{brand}:{model_code(value)}"))
    # ASIN identifies a purchasable Amazon item, never a general retailer SKU.
    if offer.provider == "amazon" and offer.asin:
        signals.append(("asin", normalized(offer.asin)))
    signals.append(("listing", f"{offer.store_slug}:{offer.external_id}"))
    return sorted(set(signals))


def identity_key(offer: ProductOfferData) -> str:
    identifier = trade_id(offer)
    signals = dict(identity_signals(offer))
    identity = (
        f"gtin:{identifier}"
        if identifier
        else f"model:{signals['brand_model']}"
        if "brand_model" in signals
        else f"asin:{signals['asin']}"
        if "asin" in signals
        else f"listing:{signals['listing']}"
    )
    variants = normalized_variant(offer.variant)
    raw = json.dumps([identity, variants], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    confidence: Decimal
    method: str
    reasons: tuple[str, ...] = ()


def variant_evidence(left: dict[str, str], right: dict[str, str]) -> str:
    a, b = normalized_variant(left), normalized_variant(right)
    if any(key.startswith("conflict:") for key in (*a, *b)):
        return "conflicting"
    if any(a[key] != b[key] for key in a.keys() & b.keys()):
        return "conflicting"
    return "compatible" if a == b else "unknown"


class ProductMatcher:
    def match(self, left: ProductOfferData, right: ProductOfferData) -> MatchResult:
        def reject(method: str, reason: str) -> MatchResult:
            return MatchResult(False, Decimal(0), method, (reason,))

        a, b = trade_ids(left), trade_ids(right)
        if len(a) > 1 or len(b) > 1 or (a and b and a != b):
            return reject("identifier_conflict", "conflicting_trade_identifiers")
        if left.brand and right.brand and model_code(left.brand) != model_code(right.brand):
            return reject("identifier_conflict", "conflicting_brands")
        for field in ("mpn", "model"):
            x, y = getattr(left, field), getattr(right, field)
            if x and y and model_code(x) != model_code(y):
                return reject("identifier_conflict", f"conflicting_{field}")
        if (
            left.provider == right.provider == "amazon"
            and left.asin
            and right.asin
            and normalized(left.asin) != normalized(right.asin)
        ):
            return reject("identifier_conflict", "conflicting_asin")
        variants_left = normalized_variant(left.variant)
        variants_right = normalized_variant(right.variant)
        evidence = variant_evidence(variants_left, variants_right)
        if evidence == "conflicting":
            return reject("variant_mismatch", "explicit_variant_conflict")
        if evidence == "unknown":
            # GTIN can supply optional size/color evidence. Opaque variation IDs and
            # condition/size-system boundaries still require an explicit agreement.
            missing = variants_left.keys() ^ variants_right.keys()
            optional = {"color", "size", "capacity"}
            if not (a and a == b and missing <= optional):
                return reject("variant_mismatch", "missing_variant_evidence")
        # Negative title evidence can veto a merge, but never authorizes one.
        capacities = [
            set(re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:gb|tb)\b", normalized(x.title)))
            for x in (left, right)
        ]
        capacities = [{re.sub(r"\s+", "", v) for v in values} for values in capacities]
        if capacities[0] and capacities[1] and capacities[0] != capacities[1]:
            return reject("variant_mismatch", "title_capacity_conflict")
        sizes = [
            set(
                re.findall(
                    r"\b(?:size|taille|grösse|talla|taglia|rozmiar|размер)\s*:?\s*(\d+(?:[.,]\d+)?)\b",
                    normalized(x.title),
                )
            )
            for x in (left, right)
        ]
        if sizes[0] and sizes[1] and sizes[0] != sizes[1]:
            return reject("variant_mismatch", "title_size_conflict")
        if a and b:
            return MatchResult(
                True, Decimal("1"), "gtin", ("same_trade_identifier", f"variant_{evidence}")
            )
        if left.store_slug == right.store_slug and left.external_id == right.external_id:
            return MatchResult(True, Decimal("1"), "listing_identity", ("same_merchant_listing",))
        common = set(identity_signals(left)) & set(identity_signals(right))
        if any(kind == "brand_model" for kind, _ in common):
            method = "manufacturer_model" if left.mpn or right.mpn else "brand_model"
            return MatchResult(True, Decimal("0.95"), method, ("same_brand_and_model",))
        if any(kind == "asin" for kind, _ in common):
            return MatchResult(True, Decimal("0.98"), "asin", ("same_amazon_item",))
        return reject("insufficient_evidence", "title_similarity_cannot_authorize_merge")
