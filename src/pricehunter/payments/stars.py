from aiogram import Bot
from aiogram.types import LabeledPrice, StarAmount, StarTransaction

from pricehunter.domain.billing import STARS_PERIOD, parse_checkout_payload
from pricehunter.domain.errors import BillingUnavailableError, PaymentRejectedError
from pricehunter.payments.base import CheckoutContext, PaymentProvider


class TelegramStarsPaymentProvider(PaymentProvider):
    name = "telegram_stars"
    currency = "XTR"
    subscription_period = STARS_PERIOD

    def __init__(self, bot: Bot | None) -> None:
        self.bot = bot

    def client(self) -> Bot:
        if self.bot is None:
            raise BillingUnavailableError()
        return self.bot

    async def create_checkout(self, context: CheckoutContext) -> str:
        product = context.product
        if context.platform != "telegram" or product is None or not product.active:
            raise PaymentRejectedError()
        parse_checkout_payload(context.payload)
        if context.plan != product.plan:
            raise PaymentRejectedError()
        return await self.client().create_invoice_link(
            title=context.title,
            description=context.description,
            payload=context.payload,
            currency=self.currency,
            prices=[LabeledPrice(label=context.title, amount=product.stars)],
            subscription_period=product.subscription_period,
            provider_token="",
            request_timeout=10,
        )

    async def refund(self, telegram_user_id: int, charge_id: str) -> None:
        if not await self.client().refund_star_payment(
            user_id=telegram_user_id,
            telegram_payment_charge_id=charge_id,
            request_timeout=15,
        ):
            raise BillingUnavailableError()

    async def cancel_renewal(self, telegram_user_id: int, charge_id: str) -> None:
        if not await self.client().edit_user_star_subscription(
            user_id=telegram_user_id,
            telegram_payment_charge_id=charge_id,
            is_canceled=True,
            request_timeout=15,
        ):
            raise BillingUnavailableError()

    async def transactions(self, offset: int, limit: int = 100) -> list[StarTransaction]:
        result = await self.client().get_star_transactions(
            offset=offset,
            limit=limit,
            request_timeout=20,
        )
        return result.transactions

    async def balance(self) -> StarAmount:
        return await self.client().get_my_star_balance(request_timeout=15)
