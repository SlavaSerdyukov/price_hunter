from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import NotificationEvent, Product, ProductWatch, Store, StoreOffer, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import ComparisonOffer, currency_comparisons
from pricehunter.domain.errors import ProductNotFoundError, SubscriptionLimitReachedError
from pricehunter.domain.subscriptions import Feature
from pricehunter.schemas.watches import WatchCreate, WatchPatch, WatchView
from pricehunter.services.comparison_service import eligible_offers
from pricehunter.services.entitlement_service import EntitlementService


class ProductWatchService:
    def __init__(
        self, sessions: SessionFactory, entitlements: EntitlementService, settings: Settings
    ) -> None:
        self.sessions, self.entitlements, self.settings = sessions, entitlements, settings

    async def create(self, user_id: UUID, data: WatchCreate) -> WatchView:
        async with self.sessions.begin() as session:
            if await session.get(User, user_id, with_for_update=True) is None:
                raise ProductNotFoundError()
            product = await session.get(Product, data.product_id, with_for_update=True)
            if product is None:
                raise ProductNotFoundError()
            rights = (await self.entitlements.for_user(user_id, session=session)).entitlements
            rights.require(Feature.COMPARISON_SEARCH)
            if data.target_price is not None:
                rights.require(Feature.TARGET_ALERTS)
            existing = await session.scalar(
                select(ProductWatch).where(
                    ProductWatch.user_id == user_id,
                    ProductWatch.product_id == product.id,
                    ProductWatch.currency == data.currency,
                )
            )
            if existing:
                return await self._view(session, existing, product)
            if await self.entitlements.stored_count(session, user_id) >= rights.max_trackers:
                raise SubscriptionLimitReachedError()
            offers = await eligible_offers(session, product.id, data.currency)
            if not offers:
                raise ProductNotFoundError()
            best = currency_comparisons(offers)[0].best_available_offer
            watch = ProductWatch(
                user_id=user_id,
                **data.model_dump(),
                best_offer_id=best.offer_id if best else None,
                best_price=best.price if best else None,
            )
            session.add(watch)
            await session.flush()
            await self._schedule(session, watch, rights.check_interval_seconds)
            return await self._view(session, watch, product)

    async def _schedule(self, session: AsyncSession, watch: ProductWatch, interval: int) -> None:
        # A faster new watch must not wait for a previous slower subscriber's schedule.
        rows = await session.execute(
            select(StoreOffer, Store.provider_type)
            .join(Store)
            .where(StoreOffer.product_id == watch.product_id, StoreOffer.currency == watch.currency)
            .order_by(StoreOffer.id)
            .with_for_update(of=StoreOffer)
        )
        now = utcnow()
        for offer, provider in rows:
            delay = self.settings.mock_check_interval_seconds if provider == "mock" else interval
            offer.next_check_at = min(
                offer.next_check_at, max(now, offer.last_checked_at + timedelta(seconds=delay))
            )

    async def _view(
        self, session: AsyncSession, watch: ProductWatch, product: Product
    ) -> WatchView:
        scheduled = await session.scalar(
            select(ProductWatch.id).where(
                ProductWatch.id == watch.id,
                ProductWatch.id.in_(self.entitlements.scheduled_watch_ids(utcnow())),
            )
        )
        return WatchView(
            id=watch.id,
            product_id=product.id,
            canonical_name=product.canonical_name,
            currency=watch.currency,
            target_price=watch.target_price,
            enabled=watch.enabled,
            notify_on_new_best=watch.notify_on_new_best,
            notify_on_price_drop=watch.notify_on_price_drop,
            best_offer_id=watch.best_offer_id,
            best_price=watch.best_price,
            scheduled=scheduled is not None,
        )

    async def list(self, user_id: UUID, page: int = 0, size: int = 10) -> list[WatchView]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ProductWatch, Product)
                    .join(Product)
                    .where(
                        ProductWatch.user_id == user_id,
                    )
                    .order_by(ProductWatch.created_at, ProductWatch.id)
                    .offset(page * size)
                    .limit(size)
                )
            ).all()
            return [await self._view(session, watch, product) for watch, product in rows]

    async def update(self, user_id: UUID, watch_id: UUID, patch: WatchPatch) -> WatchView:
        async with self.sessions.begin() as session:
            await session.get(User, user_id, with_for_update=True)
            watch = await session.scalar(
                select(ProductWatch).where(
                    ProductWatch.id == watch_id,
                    ProductWatch.user_id == user_id,
                )
            )
            if watch is None:
                raise ProductNotFoundError()
            product = await session.get(Product, watch.product_id, with_for_update=True)
            assert product is not None
            await session.refresh(watch, with_for_update=True)
            rights = (await self.entitlements.for_user(user_id, session=session)).entitlements
            if patch.target_price:
                rights.require(Feature.TARGET_ALERTS)
            if patch.enabled:
                rights.require(Feature.COMPARISON_SEARCH)
            for key, value in patch.model_dump(exclude_unset=True).items():
                setattr(watch, key, value)
            if patch.enabled is not None:
                await session.execute(
                    delete(NotificationEvent).where(
                        NotificationEvent.product_watch_id == watch.id,
                        NotificationEvent.status == "pending",
                    )
                )
                offers = await eligible_offers(session, product.id, watch.currency)
                best = currency_comparisons(offers)[0].best_available_offer if offers else None
                watch.best_offer_id, watch.best_price = (
                    (best.offer_id, best.price) if best else (None, None)
                )
                if patch.enabled:
                    await self._schedule(session, watch, rights.check_interval_seconds)
            await session.flush()
            return await self._view(session, watch, product)

    async def delete(self, user_id: UUID, watch_id: UUID) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ProductWatch).where(
                    ProductWatch.id == watch_id,
                    ProductWatch.user_id == user_id,
                )
            )

    async def evaluate(self, session: AsyncSession, product_id: UUID) -> None:
        """Caller holds the Product lock; observation/state/outbox share its transaction."""
        now = utcnow()
        if not await session.scalar(
            select(ProductWatch.id)
            .where(
                ProductWatch.product_id == product_id,
                ProductWatch.enabled.is_(True),
            )
            .limit(1)
        ):
            return
        watches = list(
            await session.scalars(
                select(ProductWatch)
                .where(
                    ProductWatch.product_id == product_id,
                    ProductWatch.id.in_(self.entitlements.scheduled_watch_ids(now)),
                )
                .order_by(ProductWatch.id)
                .with_for_update()
            )
        )
        if not watches:
            return
        product = await session.get(Product, product_id)
        assert product is not None
        offers = await eligible_offers(session, product_id)
        groups = {g.currency: g for g in currency_comparisons(offers)}
        for watch in watches:
            best = groups[watch.currency].best_available_offer if watch.currency in groups else None
            previous_id, previous_price = watch.best_offer_id, watch.best_price
            if (previous_id, previous_price) == (
                (best.offer_id, best.price) if best else (None, None)
            ):
                continue
            watch.best_offer_id, watch.best_price = (
                (best.offer_id, best.price) if best else (None, None)
            )
            watch.evaluation_sequence += 1
            if best is None:
                continue
            rights = (
                await self.entitlements.for_user(watch.user_id, session=session, now=now)
            ).entitlements
            if not rights.comparison_search:
                continue
            event_type = self._event(
                watch,
                best,
                previous_id,
                previous_price,
                rights.target_price_alerts,
                rights.back_in_stock_alerts,
            )
            if event_type is None:
                continue
            if (
                event_type == "new_best_price"
                and watch.last_notified_at
                and watch.last_notified_at
                > now - timedelta(seconds=self.settings.notification_cooldown_seconds)
            ):
                continue
            previous = await session.get(StoreOffer, previous_id) if previous_id else None
            previous_store = next((o.store for o in offers if o.offer_id == previous_id), "—")
            await session.execute(
                insert(NotificationEvent)
                .values(
                    id=uuid4(),
                    product_watch_id=watch.id,
                    event_type=event_type,
                    price=best.price,
                    currency=watch.currency,
                    deduplication_key=f"watch:{watch.id}:{watch.evaluation_sequence}",
                    snapshot={
                        "product_id": str(product_id),
                        "title": product.canonical_name,
                        "offer_id": str(best.offer_id),
                        "store": best.store,
                        "country": best.store_country,
                        "url": best.url,
                        "previous_price": str(previous_price)
                        if previous_price is not None
                        else None,
                        "previous_store": previous_store if previous else "—",
                    },
                )
                .on_conflict_do_nothing(index_elements=[NotificationEvent.deduplication_key])
            )
            watch.last_notified_at = now

    @staticmethod
    def _event(
        watch: ProductWatch,
        best: ComparisonOffer,
        previous_id: UUID | None,
        previous_price: Decimal | None,
        targets_allowed: bool,
        restocks_allowed: bool,
    ) -> str | None:
        if (
            targets_allowed
            and watch.target_price
            and best.price <= watch.target_price
            and (previous_price is None or previous_price > watch.target_price)
        ):
            return "target_reached"
        if previous_id is None:
            return (
                "best_offer_back_in_stock"
                if watch.notify_on_new_best and restocks_allowed
                else None
            )
        if previous_id != best.offer_id and watch.notify_on_new_best:
            return "merchant_became_cheapest"
        if (
            previous_price is not None
            and best.price < previous_price
            and watch.notify_on_price_drop
        ):
            return "new_best_price"
        return None
