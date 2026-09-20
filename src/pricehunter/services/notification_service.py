from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import NotificationEvent, ProductWatch, StoreOffer, Tracker, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.subscriptions import Feature
from pricehunter.services.entitlement_service import EntitlementService


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
    product_id: UUID | None = None
    store: str = ""
    previous_price: Decimal | None = None
    previous_store: str = ""


class RetryDelivery(Exception):
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds


class PermanentDeliveryFailure(Exception):
    pass


class NotificationSender(Protocol):
    async def send(self, delivery: Delivery) -> int: ...


class NotificationService:
    def __init__(
        self, sessions: SessionFactory, sender: NotificationSender, entitlements: EntitlementService
    ) -> None:
        self.sessions, self.sender, self.entitlements = sessions, sender, entitlements

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
            event = await session.scalar(
                select(NotificationEvent)
                .where(
                    NotificationEvent.status == "pending",
                    NotificationEvent.available_at <= utcnow(),
                )
                .order_by(NotificationEvent.available_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            if event.product_watch_id:
                watch = await session.get(ProductWatch, event.product_watch_id)
                assert watch is not None
                user = await session.get(User, watch.user_id)
                eligible = await session.scalar(
                    select(ProductWatch.id).where(
                        ProductWatch.id == watch.id,
                        ProductWatch.id.in_(self.entitlements.scheduled_watch_ids(utcnow())),
                    )
                )
                snapshot = event.snapshot
                title, url = str(snapshot["title"]), str(snapshot["url"])
                product_id = watch.product_id
            else:
                assert event.tracker_id is not None
                tracker = await session.get(Tracker, event.tracker_id)
                assert tracker is not None
                user = await session.get(User, tracker.user_id)
                offer = await session.get(StoreOffer, tracker.store_offer_id)
                assert offer is not None
                eligible = await session.scalar(
                    select(Tracker.id).where(
                        Tracker.id == tracker.id,
                        Tracker.id.in_(self.entitlements.scheduled_tracker_ids(utcnow())),
                    )
                )
                title, url, product_id, snapshot = (
                    offer.title,
                    offer.affiliate_url or offer.direct_url,
                    None,
                    {},
                )
            assert user is not None
            rights = (await self.entitlements.for_user(user.id, session=session)).entitlements
            feature = {
                "target_reached": Feature.TARGET_ALERTS,
                "historical_low": Feature.HISTORICAL_LOW,
                "back_in_stock": Feature.BACK_IN_STOCK,
                "best_offer_back_in_stock": Feature.BACK_IN_STOCK,
            }.get(event.event_type)
            if (
                not eligible
                or user.telegram_user_id is None
                or (product_id is not None and not rights.comparison_search)
                or (feature is not None and not rights.allows(feature))
            ):
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
                title=title,
                price=event.price,
                currency=event.currency,
                url=url,
                product_id=product_id,
                store=str(snapshot.get("store", "")),
                previous_price=Decimal(snapshot["previous_price"])
                if snapshot.get("previous_price")
                else None,
                previous_store=str(snapshot.get("previous_store", "")),
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
