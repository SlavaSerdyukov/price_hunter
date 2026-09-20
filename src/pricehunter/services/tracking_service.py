from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import NotificationEvent, Store, StoreOffer, Tracker, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import ProductNotFoundError, SubscriptionLimitReachedError
from pricehunter.domain.subscriptions import Feature
from pricehunter.schemas.api import OfferView, TrackerCreate, TrackerPatch, TrackerView
from pricehunter.services.entitlement_service import EntitlementService


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
        entitlements: EntitlementService,
        settings: Settings,
    ) -> None:
        self.sessions, self.entitlements, self.settings = sessions, entitlements, settings

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
                return await self._view(session, existing, offer)
            limits = (await self.entitlements.for_user(user_id, session=session)).entitlements
            if data.target_price is not None:
                limits.require(Feature.TARGET_ALERTS)
            count = await self.entitlements.stored_count(session, user_id)
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
            return await self._view(session, tracker, offer)

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
            records = list(rows)
            views = [tracker_view(tracker, offer) for tracker, offer in records]
            limits = (await self.entitlements.for_user(user_id, session=session)).entitlements
            stores = {
                store.id: store.provider_type
                for store in await session.scalars(
                    select(Store).where(Store.id.in_([offer.store_id for _, offer in records]))
                )
            }
            allowed = set(
                await session.scalars(
                    select(Tracker.id).where(
                        Tracker.user_id == user_id,
                        Tracker.id.in_(self.entitlements.scheduled_tracker_ids(utcnow())),
                    )
                )
            )
            for view, (_, offer) in zip(views, records, strict=True):
                view.scheduled = view.id in allowed
                view.check_interval_seconds = (
                    self.settings.mock_check_interval_seconds
                    if stores[offer.store_id] == "mock"
                    else limits.check_interval_seconds
                )
            return views

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
            return await self._view(session, row[0], row[1])

    async def update(self, user_id: UUID, tracker_id: UUID, patch: TrackerPatch) -> TrackerView:
        async with self.sessions.begin() as session:
            await session.get(User, user_id, with_for_update=True)
            limits = (await self.entitlements.for_user(user_id, session=session)).entitlements
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
            fields = patch.model_dump(exclude_unset=True)
            for field, feature in {
                "target_price": Feature.TARGET_ALERTS,
                "notify_on_target": Feature.TARGET_ALERTS,
                "notify_on_back_in_stock": Feature.BACK_IN_STOCK,
                "notify_on_historical_low": Feature.HISTORICAL_LOW,
            }.items():
                if fields.get(field):
                    limits.require(feature)
            for field, value in fields.items():
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
            return await self._view(session, tracker, offer)

    async def _view(
        self, session: AsyncSession, tracker: Tracker, offer: StoreOffer
    ) -> TrackerView:
        view = tracker_view(tracker, offer)
        view.scheduled = bool(
            await session.scalar(
                select(Tracker.id).where(
                    Tracker.id == tracker.id,
                    Tracker.id.in_(self.entitlements.scheduled_tracker_ids(utcnow())),
                )
            )
        )
        limits = (await self.entitlements.for_user(tracker.user_id, session=session)).entitlements
        store = await session.get(Store, offer.store_id)
        view.check_interval_seconds = (
            self.settings.mock_check_interval_seconds
            if store and store.provider_type == "mock"
            else limits.check_interval_seconds
        )
        return view

    async def delete(self, user_id: UUID, tracker_id: UUID) -> None:
        async with self.sessions.begin() as session:
            # Deleting twice is safe; the ownership predicate is always required.
            await session.execute(
                delete(Tracker).where(
                    Tracker.id == tracker_id,
                    Tracker.user_id == user_id,
                )
            )
