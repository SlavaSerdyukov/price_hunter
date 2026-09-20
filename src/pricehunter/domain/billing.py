import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pricehunter.domain.errors import PaymentRejectedError
from pricehunter.domain.subscriptions import Plan

STARS_PROVIDER = "telegram_stars"
STARS_PERIOD = 2592000


class CheckoutStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class BillingEventType(StrEnum):
    PURCHASE = "successful_payment"
    REFUND = "refund"


@dataclass(frozen=True)
class BillingProduct:
    code: str
    plan: Plan
    stars: int
    subscription_period: int = STARS_PERIOD
    active: bool = True

    def __post_init__(self) -> None:
        if self.plan == Plan.FREE or not 1 <= self.stars <= 10000:
            raise ValueError("Invalid subscription product")
        if self.subscription_period != STARS_PERIOD:
            raise ValueError("Telegram requires a 30-day subscription period")


def checkout_payload(intent_id: UUID) -> str:
    return "ph2:" + intent_id.hex


def parse_checkout_payload(payload: str) -> UUID:
    if not re.fullmatch(r"ph2:[0-9a-f]{32}", payload):
        raise PaymentRejectedError()
    return UUID(hex=payload[4:])


@dataclass(frozen=True)
class StarsPayment:
    telegram_user_id: int
    charge_id: str
    payload: str
    currency: str
    amount: int
    paid_at: datetime
    expiration: datetime | None = None
    recurring: bool = False
    first_recurring: bool = False


@dataclass(frozen=True)
class Checkout:
    intent_id: UUID
    url: str
    product: BillingProduct


@dataclass(frozen=True)
class PaymentResult:
    applied: bool
    plan: Plan
    valid_until: datetime
