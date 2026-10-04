from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.base import utcnow
from pricehunter.db.models import PriceObservation, StoreOffer
from pricehunter.domain.discovery import DiscoveryMismatch
from pricehunter.domain.ingestion import FeedRevalidationContext
from pricehunter.domain.products import ProductOfferData
from pricehunter.domain.provider_policy import ProviderDataPolicy


class SnapshotUpdater:
    """Mutable listing state only; caller resolves identity and holds Product/offer locks."""

    async def accept(
        self,
        session: AsyncSession,
        offer: StoreOffer,
        data: ProductOfferData,
        policy: ProviderDataPolicy,
        *,
        revalidation: FeedRevalidationContext | None = None,
    ) -> bool:
        policy.require("catalog_persistence_allowed")
        if (offer.market_country, offer.currency, offer.external_id) != (
            data.country,
            data.currency,
            data.external_id,
        ):
            raise DiscoveryMismatch()
        now = utcnow()
        if revalidation is not None and (
            revalidation.program_id != offer.merchant_program_id
            or revalidation.program_id != data.merchant_program_id
            or revalidation.external_id != data.external_id
            or revalidation.generation != data.feed_generation
            or revalidation.confirmed_at > now
        ):
            raise DiscoveryMismatch()
        if offer.source_updated_at is not None and (
            data.source_updated_at is None or data.source_updated_at <= offer.source_updated_at
        ):
            if revalidation is not None:
                # Current feed presence can renew availability, never overwrite newer content.
                offer.last_checked_at = revalidation.confirmed_at
                offer.catalog_active = True
                offer.feed_generation = revalidation.generation
                await session.flush()
            return False
        if data.source_updated_at is not None and data.source_updated_at > now:
            raise DiscoveryMismatch()
        changed = (offer.price, offer.availability) != (data.price, data.availability)
        if changed and policy.price_history_allowed:
            session.add(
                PriceObservation(
                    store_offer_id=offer.id,
                    refresh_key=f"snapshot:{uuid4()}",
                    price=data.price,
                    currency=data.currency,
                    availability=data.availability,
                    checked_at=now,
                )
            )
            offer.minimum_price = min(offer.minimum_price, data.price)
            offer.maximum_price = max(offer.maximum_price, data.price)
            offer.total_price += data.price
            offer.observation_count += 1
        elif not policy.price_history_allowed:
            offer.minimum_price = offer.maximum_price = offer.total_price = data.price
            offer.observation_count = 1
        for field in (
            "price",
            "original_price",
            "availability",
            "title",
            "image_url",
            "url",
            "direct_url",
            "affiliate_url",
            "affiliate_network",
            "affiliate_metadata",
            "seller",
            "sku",
        ):
            setattr(offer, field, getattr(data, field))
        for field, value in data.delivery_values().items():
            setattr(offer, field, value)
        offer.metadata_json = data.metadata
        offer.source_updated_at = data.source_updated_at
        offer.last_checked_at = revalidation.confirmed_at if revalidation else now
        offer.catalog_active = True
        offer.feed_generation = max(offer.feed_generation, data.feed_generation)
        await session.flush()
        return True
