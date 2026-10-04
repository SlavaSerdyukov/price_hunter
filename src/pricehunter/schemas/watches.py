from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from pricehunter.domain.markets import CountryCode
from pricehunter.domain.products import Money
from pricehunter.schemas.api import InputModel


class WatchCreate(InputModel):
    product_id: UUID
    market_country: CountryCode | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_price: Money | None = None
    notify_on_new_best: bool = True
    notify_on_price_drop: bool = True


class WatchPatch(InputModel):
    target_price: Money | None = None
    enabled: bool | None = None
    notify_on_new_best: bool | None = None
    notify_on_price_drop: bool | None = None

    @field_validator("enabled", "notify_on_new_best", "notify_on_price_drop")
    @classmethod
    def reject_null_flags(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Boolean flags cannot be null")
        return value


class WatchView(WatchCreate):
    market_country: CountryCode
    id: UUID
    canonical_name: str
    enabled: bool
    scheduled: bool
    best_offer_id: UUID | None
    best_merchant_id: UUID | None = None
    best_price: Decimal | None
