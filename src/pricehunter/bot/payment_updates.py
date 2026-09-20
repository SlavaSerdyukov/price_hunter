from datetime import UTC, datetime

from aiogram.types import Message, Update

from pricehunter.core.container import Container
from pricehunter.domain.billing import StarsPayment
from pricehunter.domain.errors import PaymentRejectedError


def stars_payment(message: Message) -> StarsPayment:
    paid, refund = message.successful_payment, message.refunded_payment
    if paid is not None and message.from_user is not None:
        return StarsPayment(
            message.from_user.id,
            paid.telegram_payment_charge_id,
            paid.invoice_payload,
            paid.currency,
            paid.total_amount,
            message.date,
            datetime.fromtimestamp(paid.subscription_expiration_date, UTC)
            if paid.subscription_expiration_date is not None
            else None,
            bool(paid.is_recurring),
            bool(paid.is_first_recurring),
        )
    if refund is not None:
        return StarsPayment(
            message.chat.id,
            refund.telegram_payment_charge_id,
            refund.invoice_payload,
            refund.currency,
            refund.total_amount,
            message.date,
        )
    raise PaymentRejectedError()


async def persist_financial_update(container: Container, update: Update) -> None:
    message = update.message
    if message is not None and (message.successful_payment or message.refunded_payment):
        if message.chat.type != "private":
            raise PaymentRejectedError()
        await container.billing_intake.receive(
            stars_payment(message), refund=message.refunded_payment is not None
        )
