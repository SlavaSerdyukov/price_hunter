from datetime import datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from pricehunter.db.base import utcnow
from pricehunter.db.models import BillingUpdate
from pricehunter.db.session import SessionFactory
from pricehunter.domain.billing import BillingEventType, PaymentResult, StarsPayment
from pricehunter.domain.errors import PaymentRejectedError
from pricehunter.services.billing_service import BillingService, charge_reference


class BillingIntake:
    """Durable, minimal Telegram financial inbox; commit before transport acknowledges."""

    def __init__(self, sessions: SessionFactory, billing: BillingService) -> None:
        self.sessions, self.billing = sessions, billing

    async def receive(self, payment: StarsPayment, *, refund: bool = False) -> UUID:
        if not 1 <= len(payment.charge_id) <= 200 or len(payment.payload) > 128:
            raise PaymentRejectedError()
        event_type = BillingEventType.REFUND if refund else BillingEventType.PURCHASE
        data = {
            "telegram_user_id": payment.telegram_user_id,
            "payload": payment.payload,
            "currency": payment.currency,
            "amount": payment.amount,
            "paid_at": payment.paid_at.isoformat(),
            "expiration": payment.expiration.isoformat() if payment.expiration else None,
            "recurring": payment.recurring,
            "first_recurring": payment.first_recurring,
        }
        async with self.sessions.begin() as session:
            identity = await session.scalar(
                insert(BillingUpdate)
                .values(
                    event_type=event_type,
                    charge_id=payment.charge_id,
                    data=data,
                )
                .on_conflict_do_nothing(
                    index_elements=[BillingUpdate.event_type, BillingUpdate.charge_id]
                )
                .returning(BillingUpdate.id)
            )
            if identity is not None:
                return identity
            existing = await session.scalar(
                select(BillingUpdate).where(
                    BillingUpdate.event_type == event_type,
                    BillingUpdate.charge_id == payment.charge_id,
                )
            )
            assert existing is not None
            if existing.data != data:
                raise PaymentRejectedError()
            return existing.id

    async def process(self, identity: UUID) -> PaymentResult | None:
        async with self.sessions() as session:
            row = await session.get(BillingUpdate, identity)
            if row is None or row.status != "pending":
                return None
            data, charge_id, event_type = row.data, row.charge_id, row.event_type
        result: PaymentResult | None = None
        status, error = "processed", None
        try:
            if event_type == BillingEventType.REFUND:
                await self.billing.record_refund(
                    data["telegram_user_id"],
                    charge_id,
                    amount=data["amount"],
                    currency=data["currency"],
                    payload=data["payload"],
                )
            else:
                result = await self.billing.process_successful_payment(
                    StarsPayment(
                        telegram_user_id=data["telegram_user_id"],
                        charge_id=charge_id,
                        payload=data["payload"],
                        currency=data["currency"],
                        amount=data["amount"],
                        paid_at=datetime.fromisoformat(data["paid_at"]),
                        expiration=datetime.fromisoformat(data["expiration"])
                        if data["expiration"]
                        else None,
                        recurring=data["recurring"],
                        first_recurring=data["first_recurring"],
                    )
                )
        except PaymentRejectedError:
            status, error = "rejected", "payment_rejected"
        except Exception as exc:
            status, error = "pending", type(exc).__name__
        async with self.sessions.begin() as session:
            row = await session.get(BillingUpdate, identity, with_for_update=True)
            if row is not None and row.status == "pending":
                row.status, row.error_code = status, error
                row.attempts += 1
                row.available_at = utcnow() + timedelta(
                    seconds=min(3600, 30 * 2 ** min(row.attempts, 7))
                )
        if error:
            structlog.get_logger().warning(
                "billing_intake_" + status,
                event_type=event_type,
                charge_ref=charge_reference(charge_id)[:12],
                error_type=error,
            )
        return result

    async def status(self, identity: UUID) -> str:
        async with self.sessions() as session:
            status = await session.scalar(
                select(BillingUpdate.status).where(BillingUpdate.id == identity)
            )
            return status or "pending"

    async def retry_pending(self, limit: int = 50) -> int:
        async with self.sessions.begin() as session:
            rows = list(
                await session.scalars(
                    select(BillingUpdate)
                    .where(
                        BillingUpdate.status == "pending",
                        BillingUpdate.available_at <= utcnow(),
                    )
                    .order_by(BillingUpdate.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            identities = [row.id for row in rows]
            for row in rows:
                row.available_at = utcnow() + timedelta(minutes=2)
        for identity in identities:
            await self.process(identity)
        return len(identities)
