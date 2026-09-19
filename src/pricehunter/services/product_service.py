from uuid import UUID

from sqlalchemy import select

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.models import PriceObservation, Product, StoreOffer, Tracker
from pricehunter.db.repositories.catalog import CatalogRepository
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.pricing import percentage_change
from pricehunter.domain.products import ProductOfferData
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.schemas.api import HistoryView, ObservationView, OfferView


class ProductService:
    def __init__(
        self,
        sessions: SessionFactory,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        settings: Settings,
    ) -> None:
        self.sessions, self.registry = sessions, registry
        self.limiter, self.settings = limiter, settings

    async def resolve(self, url: str, user_id: UUID) -> OfferView:
        await self.limiter.user(user_id)
        provider = self.registry.resolve_url(url)
        async with self.limiter.provider(provider.name):
            data = await provider.resolve_url(url)
        return await self.persist(data)

    async def persist(self, data: ProductOfferData) -> OfferView:
        async with self.sessions.begin() as session:
            offer = await CatalogRepository(session).save_resolved(data)
            await session.flush()
            return OfferView.model_validate(offer)

    async def offer(self, offer_id: UUID) -> OfferView:
        async with self.sessions() as session:
            return OfferView.model_validate(await CatalogRepository(session).get_offer(offer_id))

    async def product(self, product_id: UUID) -> tuple[str, list[OfferView]]:
        async with self.sessions() as session:
            product = await session.get(Product, product_id)
            if product is None:
                raise ProductNotFoundError()
            offers = await session.scalars(
                select(StoreOffer)
                .where(
                    StoreOffer.product_id == product_id,
                )
                .order_by(StoreOffer.currency, StoreOffer.price)
                .limit(100)
            )
            return product.canonical_name, [OfferView.model_validate(offer) for offer in offers]

    async def history(self, offer_id: UUID, user_id: UUID, limit: int = 100) -> HistoryView:
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
                    )
                    .order_by(PriceObservation.checked_at.desc())
                    .limit(min(max(limit, 1), 500))
                )
            )
            return HistoryView(
                current=offer.price,
                minimum=offer.minimum_price,
                maximum=offer.maximum_price,
                average=offer.total_price / offer.observation_count,
                currency=offer.currency,
                count=offer.observation_count,
                change_since_tracking=percentage_change(tracker.baseline_price, offer.price)
                if tracker
                else None,
                observations=[
                    ObservationView.model_validate(row) for row in reversed(observations)
                ],
                retention_days=self.settings.history_retention_days,
            )
