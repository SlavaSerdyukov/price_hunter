from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    NotificationEvent,
    Product,
    ProductBestState,
    ProductWatch,
    Store,
    StoreOffer,
    User,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import ComparisonOffer, CurrencyComparison
from pricehunter.domain.errors import (
    CountryRequiredError,
    ProductNotFoundError,
    ProviderPolicyError,
    SubscriptionLimitReachedError,
)
from pricehunter.domain.subscriptions import Feature
from pricehunter.schemas.watches import WatchCreate, WatchPatch, WatchView
from pricehunter.services.best_price_service import BestPriceService
from pricehunter.services.comparison_service import ComparisonReader
from pricehunter.services.entitlement_service import EntitlementService


class ProductWatchService:
    def __init__(
        self, sessions: SessionFactory, entitlements: EntitlementService, settings: Settings
    ) -> None:
        self.sessions, self.entitlements, self.settings = sessions, entitlements, settings
        self.reader = ComparisonReader(settings, permission="tracking_allowed")
        self.best_prices = BestPriceService(sessions, entitlements, settings)

    async def create(self, user_id: UUID, data: WatchCreate) -> WatchView:
        async with self.sessions.begin() as session:
            user = await session.get(User, user_id, with_for_update=True)
            if user is None:
                raise ProductNotFoundError()
            product = await session.get(Product, data.product_id, with_for_update=True)
            if product is None:
                raise ProductNotFoundError()
            market = data.market_country or user.country_code
            if market is None:
                raise CountryRequiredError()
            rights = (await self.entitlements.for_user(user_id, session=session)).entitlements
            rights.require(Feature.COMPARISON_SEARCH)
            if data.target_price is not None:
                rights.require(Feature.TARGET_ALERTS)
            existing = await session.scalar(
                select(ProductWatch).where(
                    ProductWatch.user_id == user_id,
                    ProductWatch.product_id == product.id,
                    ProductWatch.currency == data.currency,
                    ProductWatch.market_country == market,
                )
            )
            if existing:
                return await self._view(session, existing, product)
            if await self.entitlements.stored_count(session, user_id) >= rights.max_trackers:
                raise SubscriptionLimitReachedError()
            summary = await ComparisonReader(
                self.settings, permission="tracking_allowed", market_country=market
            ).summary(session, product.id, utcnow(), data.currency)
            if not summary.groups:
                if await session.scalar(
                    select(StoreOffer.id)
                    .where(
                        StoreOffer.product_id == product.id,
                        StoreOffer.currency == data.currency,
                        StoreOffer.market_country == market,
                    )
                    .limit(1)
                ):
                    raise ProviderPolicyError()
                raise ProductNotFoundError()
            best = summary.groups[0].best_available_offer
            watch = ProductWatch(
                user_id=user_id,
                **data.model_dump(exclude={"market_country"}),
                market_country=market,
                best_offer_id=best.offer_id if best else None,
                best_price=best.price if best else None,
                best_absence_reason=self.absence(summary.groups[0]) if not best else None,
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
            .where(
                StoreOffer.product_id == watch.product_id,
                StoreOffer.currency == watch.currency,
                StoreOffer.market_country == watch.market_country,
            )
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
            market_country=watch.market_country,
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

    async def get(self, user_id: UUID, watch_id: UUID) -> WatchView:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(ProductWatch, Product)
                    .join(Product)
                    .where(
                        ProductWatch.id == watch_id,
                        ProductWatch.user_id == user_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ProductNotFoundError()
            return await self._view(session, row[0], row[1])

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
                summary = await ComparisonReader(
                    self.settings,
                    permission="tracking_allowed",
                    market_country=watch.market_country,
                ).summary(session, product.id, utcnow(), watch.currency)
                group = summary.groups[0] if summary.groups else None
                best = group.best_available_offer if group else None
                watch.best_absence_reason = self.absence(group) if not best else None
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

    @staticmethod
    def absence(group: CurrencyComparison | None) -> str:
        if (
            group is not None
            and group.fresh_out_of_stock_count > 0
            and group.fresh_out_of_stock_count == group.fresh_offer_count
            and not (group.stale_offer_count or group.failed_offer_count)
        ):
            return "out_of_stock"
        # Missing, failed or unknown stock evidence cannot establish a true stock-out.
        return "stale"

    async def evaluate(
        self,
        session: AsyncSession,
        product_id: UUID,
        source_observation_id: UUID | None = None,
        *,
        market_country: str | None = None,
    ) -> None:
        """Caller holds the Product lock; observation/state/outbox share its transaction."""
        if market_country is None:
            markets = await session.scalars(
                select(StoreOffer.market_country)
                .where(StoreOffer.product_id == product_id)
                .union(
                    select(ProductWatch.market_country).where(
                        ProductWatch.product_id == product_id
                    ),
                    select(ProductBestState.market_country).where(
                        ProductBestState.product_id == product_id,
                        ProductBestState.market_country.is_not(None),
                    ),
                )
            )
            for market in sorted(markets):
                await self.evaluate(
                    session, product_id, source_observation_id, market_country=market
                )
            return
        now = utcnow()
        reader = ComparisonReader(
            self.settings, permission="tracking_allowed", market_country=market_country
        )
        summary = await reader.summary(session, product_id, now)
        history = await ComparisonReader(
            self.settings, permission="price_history_allowed", market_country=market_country
        ).summary(session, product_id, now)
        await self.best_prices.record(
            session, product_id, history, now, source_observation_id, market_country=market_country
        )
        if not await session.scalar(
            select(ProductWatch.id)
            .where(
                ProductWatch.product_id == product_id,
                ProductWatch.market_country == market_country,
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
                    ProductWatch.market_country == market_country,
                    ProductWatch.enabled.is_(True),
                    or_(
                        ProductWatch.id.in_(self.entitlements.scheduled_watch_ids(now)),
                        ProductWatch.best_absence_reason == "market_rebuild",
                    ),
                )
                .order_by(ProductWatch.id)
                .with_for_update()
            )
        )
        if not watches:
            return
        product = await session.get(Product, product_id)
        assert product is not None
        groups = {g.currency: g for g in summary.groups}
        for watch in watches:
            best = groups[watch.currency].best_available_offer if watch.currency in groups else None
            previous_id, previous_price = watch.best_offer_id, watch.best_price
            previous_absence = watch.best_absence_reason
            watch.best_absence_reason = (
                self.absence(groups.get(watch.currency)) if not best else None
            )
            if (previous_id, previous_price) == (
                (best.offer_id, best.price) if best else (None, None)
            ):
                continue
            watch.best_offer_id, watch.best_price = (
                (best.offer_id, best.price) if best else (None, None)
            )
            if previous_absence == "market_rebuild":
                continue
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
                previous_absence,
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
            previous_store_row = await session.get(Store, previous.store_id) if previous else None
            previous_store = previous_store_row.name if previous_store_row else "—"
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
                        "market_country": watch.market_country,
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
        previous_absence: str | None = None,
    ) -> str | None:
        if (
            targets_allowed
            and watch.target_price
            and best.price <= watch.target_price
            and (previous_price is None or previous_price > watch.target_price)
        ):
            return "target_reached"
        if previous_id is None:
            if previous_absence == "stale":
                return "best_prices_refreshed" if watch.notify_on_new_best else None
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
