from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import exists, func, or_, select

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
from pricehunter.services.product_watch_service import ProductWatchService


class RefreshAccepted(BaseModel):
    accepted: bool = True
    queued_count: int


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

    async def request_refresh(self, product_id: UUID, user_id: UUID) -> RefreshAccepted:
        (await self.entitlements.for_user(user_id)).entitlements.require(Feature.COMPARISON_SEARCH)
        await self.limiter.user(user_id)
        await self.limiter.check(f"refresh-request:user:{user_id}", limit=3, seconds=3600)
        await self.limiter.check(f"refresh-request:product:{product_id}", limit=1, seconds=300)
        now = utcnow()
        async with self.sessions.begin() as session:
            if await session.get(Product, product_id) is None:
                raise ProductNotFoundError()
            offers = list(
                await session.scalars(
                    select(StoreOffer)
                    .join(Store)
                    .where(
                        *self.reader.filters(product_id),
                        Store.provider_type.in_(
                            [
                                p.name
                                for p in self.registry.providers.values()
                                if Capability.REFRESH in p.capabilities
                                and self.settings.data_policy(p.name).refresh_allowed
                                and self.settings.data_policy(p.name).tracking_allowed
                            ]
                        ),
                        self.reader.freshness_expression(now) != Freshness.FRESH,
                        or_(StoreOffer.lease_until.is_(None), StoreOffer.lease_until < now),
                    )
                    .order_by(StoreOffer.last_checked_at, StoreOffer.id)
                    .limit(self.settings.comparison_refresh_limit)
                    .with_for_update(of=StoreOffer, skip_locked=True)
                )
            )
            for offer in offers:
                offer.next_check_at, offer.refresh_requested_at = now, now
            return RefreshAccepted(queued_count=len(offers))

    async def maintain(self) -> int:
        """Bounded expiry/backfill sweep. Readers still check freshness at request time."""
        now = utcnow()
        async with self.sessions.begin() as session:
            # Correlated earliest expiry gives overdue products priority; product locks
            # have the same order as normal observation acceptance.
            expiry = (
                select(func.min(ProductBestState.next_evaluation_at))
                .where(ProductBestState.product_id == Product.id)
                .correlate(Product)
                .scalar_subquery()
            )
            ids = list(
                await session.scalars(
                    select(Product.id)
                    .where(
                        or_(
                            expiry <= now,
                            ~exists().where(ProductBestState.product_id == Product.id)
                            & exists().where(
                                ProductWatch.product_id == Product.id,
                                ProductWatch.enabled.is_(True),
                            ),
                        )
                    )
                    .order_by(expiry.nullsfirst(), Product.id)
                    .limit(self.settings.batch_size)
                    .with_for_update(of=Product, skip_locked=True)
                )
            )
            for product_id in ids:
                await self.watches.evaluate(session, product_id)
                structlog.get_logger().info("offer_became_stale", product_id=str(product_id))
            return len(ids)
