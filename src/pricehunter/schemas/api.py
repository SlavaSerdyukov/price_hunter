from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pricehunter.domain.markets import CountryCode
from pricehunter.domain.products import Money
from pricehunter.localization.languages import LanguageCode


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveRequest(InputModel):
    url: str = Field(min_length=10, max_length=2048)


class OfferView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    product_id: UUID
    market_country: CountryCode
    title: str
    store: str = ""
    attribution: str | None = None
    url: str | None
    image_url: str | None
    price: Decimal
    original_price: Decimal | None
    currency: str
    availability: str
    minimum_price: Decimal
    last_checked_at: datetime


class TrackerCreate(InputModel):
    store_offer_id: UUID
    target_price: Money | None = None


class TrackerPatch(InputModel):
    target_price: Money | None = None
    enabled: bool | None = None
    notify_on_any_drop: bool | None = None
    notify_on_target: bool | None = None
    notify_on_back_in_stock: bool | None = None
    notify_on_historical_low: bool | None = None

    @field_validator(
        "enabled",
        "notify_on_any_drop",
        "notify_on_target",
        "notify_on_back_in_stock",
        "notify_on_historical_low",
    )
    @classmethod
    def reject_null_flags(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Boolean flags cannot be null")
        return value


class TrackerView(BaseModel):
    id: UUID
    offer: OfferView
    target_price: Decimal | None
    baseline_price: Decimal
    enabled: bool
    check_interval_seconds: int
    scheduled: bool = True


class ObservationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    price: Decimal
    currency: str
    availability: str
    checked_at: datetime


class HistoryView(BaseModel):
    current: Decimal
    minimum: Decimal
    maximum: Decimal
    average: Decimal
    currency: str
    count: int
    change_since_tracking: Decimal | None = None
    observations: list[ObservationView]
    retention_days: int


class UserSettingsPatch(InputModel):
    language_code: LanguageCode | None = None
    country_code: CountryCode | None = None
    preferred_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    timezone: str | None = None

    @field_validator("language_code", "preferred_currency", "timezone")
    @classmethod
    def no_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Value cannot be null")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Unknown IANA timezone") from exc
        return value
