from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.models import Product, Store, StoreOffer
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import (
    ComparisonOffer,
    ComparisonProduct,
    currency_comparisons,
    offer_order,
)
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.subscriptions import Feature
from pricehunter.services.entitlement_service import EntitlementService


async def eligible_offers(
    session: AsyncSession, product_id: UUID, currency: str | None = None
) -> list[ComparisonOffer]:
    query = (
        select(StoreOffer, Store)
        .join(Store)
        .where(
            StoreOffer.product_id == product_id,
            Store.active.is_(True),
            Store.supported.is_(True),
        )
    )
    if currency:
        query = query.where(StoreOffer.currency == currency)
    rows = (await session.execute(query)).all()
    return sorted(
        [
            ComparisonOffer(
                offer_id=offer.id,
                store=store.name,
                store_slug=store.slug,
                store_country=store.country,
                title=offer.title,
                price=offer.price,
                currency=offer.currency,
                availability=offer.availability,
                url=offer.affiliate_url or offer.direct_url,
                image_url=offer.image_url,
                last_checked_at=offer.last_checked_at,
            )
            for offer, store in rows
        ],
        key=offer_order,
    )


class ComparisonService:
    def __init__(self, sessions: SessionFactory, entitlements: EntitlementService) -> None:
        self.sessions, self.entitlements = sessions, entitlements

    async def get(
        self,
        product_id: UUID,
        user_id: UUID,
        *,
        page: int = 0,
        size: int = 10,
    ) -> ComparisonProduct:
        (await self.entitlements.for_user(user_id)).entitlements.require(Feature.COMPARISON_SEARCH)
        async with self.sessions() as session:
            return await self.build(session, product_id, page=page, size=size)

    async def build(
        self,
        session: AsyncSession,
        product_id: UUID,
        *,
        page: int = 0,
        size: int = 10,
    ) -> ComparisonProduct:
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError()
        offers = await eligible_offers(session, product_id)
        groups = currency_comparisons(offers)
        confidences = list(
            await session.scalars(
                select(StoreOffer.match_confidence).where(StoreOffer.product_id == product_id)
            )
        )
        size, page = min(max(size, 1), 50), max(page, 0)
        return ComparisonProduct(
            id=product.id,
            canonical_name=product.canonical_name,
            brand=product.brand,
            gtin=product.gtin,
            model=product.model or product.mpn,
            variant=product.variant,
            image_url=next((o.image_url for o in offers if o.image_url), None),
            offers=offers[page * size : (page + 1) * size],
            currencies=[g.currency for g in groups],
            currency_groups=groups,
            best_available_offer=groups[0].best_available_offer if len(groups) == 1 else None,
            price_spread=groups[0].price_spread if len(groups) == 1 else None,
            match_confidence=min(confidences) if confidences else 0,
            store_count=len({o.store_slug for o in offers}),
            offer_count=len(offers),
            page=page,
            page_size=size,
        )
