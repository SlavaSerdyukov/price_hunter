from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.base import utcnow
from pricehunter.db.models import PriceObservation, Store, StoreOffer
from pricehunter.domain.errors import ProductNotFoundError, UnsupportedStoreError
from pricehunter.domain.products import ProductOfferData


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_offer(self, offer_id: UUID) -> StoreOffer:
        offer = await self.session.get(StoreOffer, offer_id)
        if offer is None:
            raise ProductNotFoundError()
        return offer

    async def save_resolved(
        self,
        data: ProductOfferData,
        product_id: UUID,
        confidence: Decimal,
        *,
        history_allowed: bool = True,
    ) -> StoreOffer:
        await self.session.execute(
            insert(Store)
            .values(
                id=uuid4(),
                slug=data.store_slug,
                name=data.store_name,
                domain=data.store_domain,
                provider_type=data.provider,
                external_merchant_id=data.external_merchant_id,
                country=data.country,
            )
            .on_conflict_do_nothing(index_elements=[Store.slug])
        )
        store = (
            await self.session.scalars(select(Store).where(Store.slug == data.store_slug))
        ).one()
        if not store.active or not store.supported:
            raise UnsupportedStoreError()
        existing = await self.session.scalar(
            select(StoreOffer).where(
                StoreOffer.store_id == store.id,
                StoreOffer.external_id == data.external_id,
            )
        )
        if existing:
            return existing
        offer_id = uuid4()
        now = utcnow()
        inserted = await self.session.scalar(
            insert(StoreOffer)
            .values(
                id=offer_id,
                product_id=product_id,
                store_id=store.id,
                external_id=data.external_id,
                url=data.url,
                direct_url=data.direct_url,
                affiliate_url=data.affiliate_url,
                affiliate_network=data.affiliate_network,
                affiliate_metadata=data.affiliate_metadata,
                title=data.title,
                image_url=data.image_url,
                price=data.price,
                original_price=data.original_price,
                currency=data.currency,
                availability=data.availability,
                seller=data.seller,
                sku=data.sku,
                metadata_json=data.metadata,
                identity_data=data.model_dump(
                    mode="json",
                    include={
                        "brand",
                        "model",
                        "mpn",
                        "gtin",
                        "ean",
                        "upc",
                        "asin",
                        "variant",
                    },
                ),
                match_confidence=confidence,
                minimum_price=data.price,
                maximum_price=data.price,
                total_price=data.price,
                observation_count=1,
                last_checked_at=now,
                next_check_at=now,
            )
            .on_conflict_do_nothing(index_elements=[StoreOffer.store_id, StoreOffer.external_id])
            .returning(StoreOffer.id)
        )
        if inserted and history_allowed:
            self.session.add(
                PriceObservation(
                    store_offer_id=offer_id,
                    refresh_key=f"initial:{offer_id}",
                    price=data.price,
                    currency=data.currency,
                    availability=data.availability,
                    checked_at=now,
                )
            )
        # Resolution never resets a tracked offer to stale provider/cache data.
        return (
            await self.session.scalars(
                select(StoreOffer).where(
                    StoreOffer.store_id == store.id,
                    StoreOffer.external_id == data.external_id,
                )
            )
        ).one()
