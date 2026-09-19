from sqlalchemy.dialects.postgresql import insert

from pricehunter.db.models import PaymentEvent
from pricehunter.db.session import SessionFactory
from pricehunter.payments.base import VerifiedPayment


class PaymentService:
    """Durable idempotent ledger primitive. Checkout/entitlement activation is M2."""

    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    async def record_verified(self, payment: VerifiedPayment) -> bool:
        async with self.sessions.begin() as session:
            event = await session.scalar(
                insert(PaymentEvent)
                .values(
                    provider=payment.provider,
                    external_event_id=payment.external_event_id,
                    user_id=payment.user_id,
                    event_type=payment.event_type,
                    amount=payment.amount,
                    currency=payment.currency,
                    status=payment.status,
                )
                .on_conflict_do_nothing(
                    index_elements=[PaymentEvent.provider, PaymentEvent.external_event_id]
                )
                .returning(PaymentEvent.id)
            )
            return event is not None
