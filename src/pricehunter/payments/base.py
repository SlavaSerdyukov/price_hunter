from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pricehunter.domain.billing import BillingProduct
from pricehunter.domain.errors import FeatureUnavailableError


@dataclass(frozen=True)
class CheckoutContext:
    user_id: UUID
    platform: Literal["telegram", "web"]
    country: str | None
    plan: str
    product_type: Literal["digital"] = "digital"
    payload: str = ""
    product: BillingProduct | None = None
    title: str = ""
    description: str = ""


@dataclass(frozen=True)
class VerifiedPayment:
    """Only create after provider authentication and amount/intent validation in M2."""

    provider: str
    external_event_id: str
    user_id: UUID
    event_type: str
    amount: Decimal
    currency: str
    status: str


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    async def create_checkout(self, context: CheckoutContext) -> str: ...


class DisabledPaymentProvider(PaymentProvider):
    async def create_checkout(self, context: CheckoutContext) -> str:
        raise FeatureUnavailableError()


class StripePaymentProvider(DisabledPaymentProvider):
    name = "stripe"


class WalletPayPaymentProvider(DisabledPaymentProvider):
    name = "wallet_pay"


def eligible_provider(name: str, context: CheckoutContext, *, enabled: bool) -> bool:
    if not enabled:
        return False
    if context.platform == "telegram" and context.product_type == "digital":
        return name == "telegram_stars"
    # Region/product/provider approval is intentionally required before M5 checkout.
    return False
