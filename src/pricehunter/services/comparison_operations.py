from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import exists, or_, select

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.base import utcnow
from pricehunter.db.models import Product, ProductBestState, ProductWatch, Store, StoreOffer
from pricehunter.db.session import SessionFactory
from pricehunter.domain.discovery import Capability
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.freshness import Freshness
from pricehunter.domain.subscriptions import Feature
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.comparison_service import ComparisonReader
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.market_context import user_market
from pricehunter.services.product_watch_service import ProductWatchService


class RefreshAccepted(BaseModel):
    accepted: bool = True
    queued_count: int
    market_country: str


class ComparisonOperations:
    def __init__(
        self,
        sessions: SessionFactory,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        settings: Settings,
        entitlements: EntitlementService,
    ) -> None:
        self.sessions, self.registry, self.limiter = sessions, registry, limiter
        self.settings, self.entitlements = settings, entitlements
        self.reader = ComparisonReader(settings)
        self.watches = ProductWatchService(sessions, entitlements, settings)

    async def request_refresh(
        self, product_id: UUID, user_id: UUID, *, market_country: str | None = None
    ) -> RefreshAccepted:
        (await self.entitlements.for_user(user_id)).entitlements.require(Feature.COMPARISON_SEARCH)
        async with self.sessions() as session:
            market = await user_market(session, user_id, market_country)
        reader = ComparisonReader(self.settings, market_country=market)
        await self.limiter.user(user_id)
        await self.limiter.check(f"refresh-request:user:{user_id}", limit=3, seconds=3600)
        await self.limiter.check(
            f"refresh-request:product:{product_id}:{market}", limit=1, seconds=300
        )
        now = utcnow()
        async with self.sessions.begin() as session:
            if await session.get(Product, product_id) is None:
                raise ProductNotFoundError()
            offers = list(
                await session.scalars(
                    select(StoreOffer)
                    .join(Store)
                    .where(
                        *reader.filters(product_id),
                        Store.provider_type.in_(
                            [
                                p.name
                                for p in self.registry.providers.values()
                                if Capability.REFRESH in p.capabilities
                                and self.settings.data_policy(p.name).refresh_allowed
                                and self.settings.data_policy(p.name).tracking_allowed
                            ]
                        ),
                        reader.freshness_expression(now) != Freshness.FRESH,
                        or_(StoreOffer.lease_until.is_(None), StoreOffer.lease_until < now),
                    )
                    .order_by(StoreOffer.last_checked_at, StoreOffer.id)
                    .limit(self.settings.comparison_refresh_limit)
                    .with_for_update(of=StoreOffer, skip_locked=True)
                )
            )
            for offer in offers:
                offer.next_check_at, offer.refresh_requested_at = now, now
            return RefreshAccepted(queued_count=len(offers), market_country=market)

    async def maintain(self) -> int:
        """Bounded expiry/backfill sweep. Readers still check freshness at request time."""
        now = utcnow()
        async with self.sessions.begin() as session:
            due = select(ProductBestState.product_id, ProductBestState.market_country).where(
                ProductBestState.market_country.is_not(None),
                ProductBestState.next_evaluation_at <= now,
            )
            missing = select(ProductWatch.product_id, ProductWatch.market_country).where(
                ProductWatch.enabled.is_(True),
                or_(
                    ProductWatch.best_absence_reason == "market_rebuild",
                    ~exists().where(
                        ProductBestState.product_id == ProductWatch.product_id,
                        ProductBestState.market_country == ProductWatch.market_country,
                        ProductBestState.currency == ProductWatch.currency,
                    ),
                ),
            )
            contexts = due.union(missing).subquery()
            candidates = (
                await session.execute(
                    select(contexts)
                    .order_by(contexts.c.product_id, contexts.c.market_country)
                    .limit(self.settings.batch_size)
                )
            ).all()
            locked = set(
                await session.scalars(
                    select(Product.id)
                    .where(Product.id.in_([c.product_id for c in candidates]))
                    .order_by(Product.id)
                    .with_for_update(skip_locked=True)
                )
            )
            count = 0
            for product_id, market in candidates:
                if product_id in locked:
                    await self.watches.evaluate(session, product_id, market_country=market)
                    structlog.get_logger().info(
                        "offer_became_stale", product_id=str(product_id), market_country=market
                    )
                    count += 1
            return count
