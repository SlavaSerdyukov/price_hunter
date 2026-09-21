from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.base import utcnow
from pricehunter.db.models import PriceObservation, Product, Tracker
from pricehunter.db.repositories.catalog import CatalogRepository
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import ComparisonProduct
from pricehunter.domain.pricing import percentage_change
from pricehunter.domain.products import ProductOfferData
from pricehunter.domain.subscriptions import Feature
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.schemas.api import HistoryView, ObservationView, OfferView
from pricehunter.services.catalog_resolver import CatalogResolver
from pricehunter.services.comparison_service import ComparisonService
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.product_watch_service import ProductWatchService


class ProductService:
    def __init__(
        self,
        sessions: SessionFactory,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        settings: Settings,
        entitlements: EntitlementService,
    ) -> None:
        self.sessions, self.registry = sessions, registry
        self.limiter, self.settings, self.entitlements = limiter, settings, entitlements
        self.resolver = CatalogResolver()
        self.watches = ProductWatchService(sessions, entitlements, settings)
        self.comparisons = ComparisonService(sessions, entitlements, settings)

    async def resolve(self, url: str, user_id: UUID) -> OfferView:
        await self.limiter.user(user_id)
        provider = self.registry.resolve_url(url)
        async with self.limiter.provider(provider.name):
            data = await provider.resolve_url(url)
        return await self.persist(data)

    async def persist(self, data: ProductOfferData) -> OfferView:
        async with self.sessions.begin() as session:
            offer = await self.resolver.resolve(session, data)
            await session.flush()
            # Resolver locks the canonical product for new listings. Re-evaluation is idempotent.
            await session.get(Product, offer.product_id, with_for_update=True)
            await self.watches.evaluate(session, offer.product_id)
            return OfferView.model_validate(offer)

    async def offer(self, offer_id: UUID) -> OfferView:
        async with self.sessions() as session:
            return OfferView.model_validate(await CatalogRepository(session).get_offer(offer_id))

    async def product(self, product_id: UUID, user_id: UUID) -> ComparisonProduct:
        return await self.comparisons.get(product_id, user_id)

    async def history(self, offer_id: UUID, user_id: UUID, limit: int = 100) -> HistoryView:
        limits = (await self.entitlements.for_user(user_id)).entitlements
        limits.require(Feature.HISTORY)
        days = limits.history_days
        if self.settings.history_retention_days:
            days = min(days, self.settings.history_retention_days)
        cutoff = utcnow() - timedelta(days=days)
        async with self.sessions() as session:
            offer = await CatalogRepository(session).get_offer(offer_id)
            tracker = await session.scalar(
                select(Tracker).where(
                    Tracker.user_id == user_id,
                    Tracker.store_offer_id == offer_id,
                )
            )
            observations = list(
                await session.scalars(
                    select(PriceObservation)
                    .where(
                        PriceObservation.store_offer_id == offer_id,
                        PriceObservation.checked_at >= cutoff,
                    )
                    .order_by(PriceObservation.checked_at.desc())
                    .limit(min(max(limit, 1), 500))
                )
            )
            stats = (
                await session.execute(
                    select(
                        func.min(PriceObservation.price),
                        func.max(PriceObservation.price),
                        func.avg(PriceObservation.price),
                        func.count(),
                    ).where(
                        PriceObservation.store_offer_id == offer_id,
                        PriceObservation.checked_at >= cutoff,
                    )
                )
            ).one()
            return HistoryView(
                current=offer.price,
                minimum=stats[0] or offer.price,
                maximum=stats[1] or offer.price,
                average=stats[2] or offer.price,
                currency=offer.currency,
                count=stats[3],
                change_since_tracking=percentage_change(tracker.baseline_price, offer.price)
                if tracker
                else None,
                observations=[
                    ObservationView.model_validate(row) for row in reversed(observations)
                ],
                retention_days=days,
            )
