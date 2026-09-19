from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import NotificationEvent, StoreOffer, Tracker, User
from pricehunter.db.session import SessionFactory


@dataclass(frozen=True)
class Delivery:
    event_id: UUID
    telegram_id: int
    language: str
    event_type: str
    title: str
    price: Decimal
    currency: str
    url: str


class RetryDelivery(Exception):
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds


class PermanentDeliveryFailure(Exception):
    pass


class NotificationSender(Protocol):
    async def send(self, delivery: Delivery) -> int: ...


class NotificationService:
    def __init__(self, sessions: SessionFactory, sender: NotificationSender) -> None:
        self.sessions, self.sender = sessions, sender

    async def claim(self) -> Delivery | None:
        async with self.sessions.begin() as session:
            # A terminated worker may have sent the message; never blindly resend it.
            await session.execute(
                update(NotificationEvent)
                .where(
                    NotificationEvent.status == "sending",
                    NotificationEvent.attempt_started_at < utcnow() - timedelta(minutes=2),
                )
                .values(status="uncertain")
            )
            row = (
                await session.execute(
                    select(NotificationEvent, Tracker, User, StoreOffer)
                    .join(Tracker, NotificationEvent.tracker_id == Tracker.id)
                    .join(User, Tracker.user_id == User.id)
                    .join(StoreOffer, Tracker.store_offer_id == StoreOffer.id)
                    .where(
                        NotificationEvent.status == "pending",
                        NotificationEvent.available_at <= utcnow(),
                    )
                    .order_by(NotificationEvent.available_at)
                    .limit(1)
                    .with_for_update(of=NotificationEvent, skip_locked=True)
                )
            ).one_or_none()
            if row is None:
                return None
            event, tracker, user, offer = row
            if not tracker.enabled or user.telegram_user_id is None:
                event.status = "cancelled"
                return None
            event.status = "sending"
            event.attempt_started_at = utcnow()
            event.attempts += 1
            return Delivery(
                event_id=event.id,
                telegram_id=user.telegram_user_id,
                language=user.language_code,
                event_type=event.event_type,
                title=offer.title,
                price=event.price,
                currency=event.currency,
                url=offer.affiliate_url or offer.direct_url,
            )

    async def send_pending(self, limit: int = 100) -> int:
        sent = 0
        for _ in range(limit):
            delivery = await self.claim()
            if delivery is None:
                break
            state, message_id, retry = "sent", None, 0
            try:
                message_id = await self.sender.send(delivery)
            except RetryDelivery as exc:
                state, retry = "pending", max(1, exc.seconds)
            except PermanentDeliveryFailure:
                state = "failed"
            except Exception as exc:
                state = "uncertain"
                structlog.get_logger().warning(
                    "delivery_uncertain",
                    event_id=str(delivery.event_id),
                    error_type=type(exc).__name__,
                )
            async with self.sessions.begin() as session:
                event = await session.get(
                    NotificationEvent, delivery.event_id, with_for_update=True
                )
                if event:
                    event.status = state if event.attempts < 8 or state != "pending" else "failed"
                    event.telegram_message_id = message_id
                    event.available_at = utcnow() + timedelta(seconds=retry)
                    if state == "sent":
                        event.sent_at = utcnow()
                        sent += 1
        return sent
