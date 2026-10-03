import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from pricehunter.domain.markets import CountryCode
from pricehunter.domain.products import Availability, Money, ProductOfferData
from pricehunter.domain.provider_policy import ProviderDataPolicy

FEED_NETWORKS = frozenset({"awin", "tradedoubler", "cj"})
AFFILIATE_HOSTS = {
    "awin": {"www.awin1.com", "awin1.com"},
    "tradedoubler": {"pdt.tradedoubler.com", "clk.tradedoubler.com", "clkuk.tradedoubler.com"},
    "cj": {"www.kqzyfj.com", "kqzyfj.com"},
}


class FeedError(Exception):
    """Stable diagnostics only; never wrap a remote URL or response body."""

    def __init__(self, code: str = "invalid_feed") -> None:
        self.code = code
        super().__init__(code)


class MerchantProgramInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    network: Literal["awin", "tradedoubler", "cj"]
    external_merchant_id: str = Field(pattern=r"^[0-9]{1,30}$")
    market_country: CountryCode
    display_name: str = Field(min_length=1, max_length=100)
    domain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,199}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    active: bool = False
    approved: bool = False
    external_feed_id: str = Field(pattern=r"^[0-9]{1,30}$")
    feed_language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    policy: ProviderDataPolicy = Field(default_factory=ProviderDataPolicy)

    @model_validator(mode="after")
    def reviewed(self) -> "MerchantProgramInput":
        if self.active and (not self.approved or not self.policy.catalog_persistence_allowed):
            raise ValueError("Active programs require approval and reviewed catalog rights")
        return self


def merchant_slug(network: str, external_id: str) -> str:
    return f"{network}-{external_id}"


class FeedProductData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    external_id: str = Field(min_length=1, max_length=200)
    parent_external_id: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    brand: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    mpn: str | None = Field(default=None, max_length=200)
    gtin: str | None = None
    ean: str | None = None
    upc: str | None = None
    variant: dict[str, str] = Field(default_factory=dict)
    price: Money
    original_price: Money | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    availability: Availability = Availability.UNKNOWN
    delivery_cost: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    affiliate_url: str = Field(min_length=1, max_length=2048)
    direct_url: str | None = Field(default=None, max_length=2048)
    image_url: str | None = Field(default=None, max_length=2048)
    source_updated_at: AwareDatetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_product(self) -> "FeedProductData":
        # Reuse existing identifier, variant and destination validation before staging.
        self.offer(
            program_id=None,
            network="awin",
            merchant_id="0",
            name="Validation",
            domain="example.com",
            market="BE",
        )
        if len(json.dumps(self.metadata)) > 8192 or len(self.variant) > 16:
            raise ValueError("Feed metadata exceeds budget")
        if any(len(k) > 100 or len(v) > 200 for k, v in self.variant.items()):
            raise ValueError("Feed variant exceeds budget")
        return self

    def offer(
        self,
        *,
        program_id: UUID | None,
        network: str,
        merchant_id: str,
        name: str,
        domain: str,
        market: str,
        generation: int = 0,
    ) -> ProductOfferData:
        fields = self.model_dump(exclude={"parent_external_id", "delivery_cost"})
        fields["metadata"] = {
            **self.metadata,
            "feed_parent_id": self.parent_external_id,
            "delivery_cost": str(self.delivery_cost) if self.delivery_cost is not None else None,
        }
        return ProductOfferData(
            **fields,
            provider=network,
            store_slug=merchant_slug(network, merchant_id),
            store_name=name,
            store_domain=domain,
            country=market,
            external_merchant_id=merchant_id,
            affiliate_network=network,
            merchant_program_id=program_id,
            feed_generation=generation,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class RejectedFeedRow:
    code: str


@dataclass(frozen=True)
class FeedReference:
    feed_id: str
    merchant_id: str
    name: str
    market_country: str | None = None
    version: str | None = None
    currency: str | None = None


class MerchantProgramCandidate(BaseModel):
    """Account visibility only; never an approval or market/data-use grant."""

    network: str
    external_merchant_id: str
    external_feed_id: str
    display_name: str
    market_country: str | None = None
    currency: str | None = None

    def template(self) -> dict[str, Any]:
        return {
            **self.model_dump(),
            "domain": "REVIEW_REQUIRED",
            "active": False,
            "approved": False,
            "policy": ProviderDataPolicy().model_dump(),
        }


class FeedReport(BaseModel):
    rows_parsed: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    duplicates: int = 0
    identifier_coverage: int = 0
    variant_coverage: int = 0
    availability_coverage: int = 0
    currencies: dict[str, int] = Field(default_factory=dict)
    would_insert: int = 0
    would_update: int = 0
    would_deactivate: int = 0
    completed_at: datetime | None = None
