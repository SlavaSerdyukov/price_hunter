from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, select

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import NotificationEvent, Store, StoreOffer, Tracker, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import ProductNotFoundError, SubscriptionLimitReachedError
from pricehunter.domain.subscriptions import SubscriptionPolicy
from pricehunter.schemas.api import OfferView, TrackerCreate, TrackerPatch, TrackerView


def tracker_view(tracker: Tracker, offer: StoreOffer) -> TrackerView:
    return TrackerView(
        id=tracker.id,
        offer=OfferView.model_validate(offer),
        target_price=tracker.target_price,
        baseline_price=tracker.baseline_price,
        enabled=tracker.enabled,
        check_interval_seconds=tracker.check_interval_seconds,
    )


class TrackingService:
    def __init__(
        self,
        sessions: SessionFactory,
        policy: SubscriptionPolicy,
        settings: Settings,
    ) -> None:
        self.sessions, self.policy, self.settings = sessions, policy, settings

    async def create(self, user_id: UUID, data: TrackerCreate) -> TrackerView:
        async with self.sessions.begin() as session:
            # Lock the user so concurrent requests cannot exceed a plan's quota.
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None:
                raise ProductNotFoundError()
            offer = await session.scalar(
                select(StoreOffer)
                .where(
                    StoreOffer.id == data.store_offer_id,
                )
                .with_for_update()
            )
            if offer is None:
                raise ProductNotFoundError()
            existing = await session.scalar(
                select(Tracker).where(
                    Tracker.user_id == user_id,
                    Tracker.store_offer_id == offer.id,
                )
            )
            if existing:
                return tracker_view(existing, offer)
            limits = self.policy.for_plan(user.subscription_plan)
            count = await session.scalar(
                select(func.count())
                .select_from(Tracker)
                .where(
                    Tracker.user_id == user_id,
                )
            )
            if (count or 0) >= limits.max_trackers:
                raise SubscriptionLimitReachedError()
            store = await session.get(Store, offer.store_id)
            interval = limits.check_interval_seconds
            if store and store.provider_type == "mock":
                interval = self.settings.mock_check_interval_seconds
            tracker = Tracker(
                user_id=user_id,
                store_offer_id=offer.id,
                target_price=data.target_price,
                baseline_price=offer.price,
                check_interval_seconds=interval,
            )
            offer.next_check_at = min(offer.next_check_at, utcnow() + timedelta(seconds=interval))
            session.add(tracker)
            await session.flush()
            return tracker_view(tracker, offer)

    async def list(self, user_id: UUID, *, page: int = 0, size: int = 5) -> list[TrackerView]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(Tracker, StoreOffer)
                .join(StoreOffer)
                .where(
                    Tracker.user_id == user_id,
                )
                .order_by(Tracker.created_at, Tracker.id)
                .offset(max(page, 0) * size)
                .limit(size)
            )
            return [tracker_view(tracker, offer) for tracker, offer in rows]

    async def get(self, user_id: UUID, tracker_id: UUID) -> TrackerView:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(Tracker, StoreOffer)
                    .join(StoreOffer)
                    .where(
                        Tracker.id == tracker_id,
                        Tracker.user_id == user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ProductNotFoundError()
            return tracker_view(row[0], row[1])

    async def update(self, user_id: UUID, tracker_id: UUID, patch: TrackerPatch) -> TrackerView:
        async with self.sessions.begin() as session:
            tracker = await session.scalar(
                select(Tracker)
                .where(
                    Tracker.id == tracker_id,
                    Tracker.user_id == user_id,
                )
                .with_for_update()
            )
            if tracker is None:
                raise ProductNotFoundError()
            for field, value in patch.model_dump(exclude_unset=True).items():
                setattr(tracker, field, value)
            if patch.enabled is False:
                await session.execute(
                    delete(NotificationEvent).where(
                        NotificationEvent.tracker_id == tracker.id,
                        NotificationEvent.status == "pending",
                    )
                )
            offer = await session.get(StoreOffer, tracker.store_offer_id)
            assert offer is not None
            await session.flush()
            return tracker_view(tracker, offer)

    async def delete(self, user_id: UUID, tracker_id: UUID) -> None:
        async with self.sessions.begin() as session:
            # Deleting twice is safe; the ownership predicate is always required.
            await session.execute(
                delete(Tracker).where(
                    Tracker.id == tracker_id,
                    Tracker.user_id == user_id,
                )
            )
