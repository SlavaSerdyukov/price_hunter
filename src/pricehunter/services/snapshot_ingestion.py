from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.base import utcnow
from pricehunter.db.models import PriceObservation, StoreOffer
from pricehunter.domain.discovery import DiscoveryMismatch
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
    ) -> bool:
        policy.require("catalog_persistence_allowed")
        if (offer.market_country, offer.currency, offer.external_id) != (
            data.country,
            data.currency,
            data.external_id,
        ):
            raise DiscoveryMismatch()
        if offer.source_updated_at is not None and (
            data.source_updated_at is None or data.source_updated_at <= offer.source_updated_at
        ):
            return False
        now = utcnow()
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
        offer.metadata_json = data.metadata
        offer.source_updated_at = data.source_updated_at
        offer.last_checked_at = now
        await session.flush()
        return True
