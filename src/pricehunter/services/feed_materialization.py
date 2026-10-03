from datetime import timedelta
from uuid import UUID

import structlog
from sqlalchemy import select

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    FeedSyncState,
    MerchantFeedItem,
    MerchantProgram,
    Product,
    StoreOffer,
)
from pricehunter.domain.discovery import DiscoveryMismatch
from pricehunter.domain.errors import ProviderPolicyError
from pricehunter.domain.ingestion import IngestionMode
from pricehunter.providers.feeds.local import materialization_data
from pricehunter.services.policy_resolver import PolicyResolver
from pricehunter.services.product_service import ProductService


class FeedMaterializationService:
    def __init__(self, products: ProductService) -> None:
        self.products = products

    async def refresh(self, program_id: UUID, *, limit: int | None = None) -> int:
        products = self.products
        async with products.sessions() as session:
            program = await session.get(MerchantProgram, program_id)
            state = await session.get(FeedSyncState, program_id)
            if (
                program is None
                or state is None
                or not PolicyResolver(products.settings)
                .program(program)
                .catalog_persistence_allowed
            ):
                return 0
            ids = list(
                await session.scalars(
                    select(StoreOffer.id)
                    .where(
                        StoreOffer.merchant_program_id == program_id,
                        StoreOffer.feed_generation < state.generation,
                    )
                    .order_by(StoreOffer.id)
                    .limit(limit or products.settings.feed_batch_size)
                )
            )
        for offer_id in ids:
            async with products.sessions.begin() as session:
                # Same Product -> Offer ordering as SnapshotUpdater and price workers.
                offer = await session.get(StoreOffer, offer_id)
                if offer is None:
                    continue
                await session.get(Product, offer.product_id, with_for_update=True)
                await session.refresh(offer, with_for_update=True)
                program = await session.get(MerchantProgram, program_id)
                state = await session.get(FeedSyncState, program_id)
                assert program is not None and state is not None
                policy = PolicyResolver(products.settings).program(program)
                item = await session.scalar(
                    select(MerchantFeedItem).where(
                        MerchantFeedItem.merchant_program_id == program_id,
                        MerchantFeedItem.external_id == offer.external_id,
                    )
                )
                if (
                    item is None
                    or not item.active
                    or item.seen_at <= utcnow() - timedelta(seconds=policy.max_cache_seconds)
                ):
                    offer.catalog_active = False
                    offer.feed_generation = state.generation
                    await session.flush()
                    await products.watches.evaluate(
                        session, offer.product_id, market_country=offer.market_country
                    )
                    continue
                data = materialization_data(item, program)
                if not policy.refresh_allowed:
                    # Catalog-only snapshots update on explicit search; background
                    # refresh of persisted listings requires its own permission.
                    offer.feed_generation = state.generation
                    continue
            # Re-enters the single identity/snapshot path with its own current locks.
            try:
                await products.persist(
                    data, mode=IngestionMode.SEARCH_SNAPSHOT, require_refresh_permission=True
                )
            except ProviderPolicyError:
                # Revocation/expiry between selection and the locked snapshot transaction.
                continue
            except DiscoveryMismatch:
                async with products.sessions.begin() as session:
                    await session.get(Product, offer.product_id, with_for_update=True)
                    rejected = await session.get(StoreOffer, offer_id, with_for_update=True)
                    if rejected is not None and rejected.feed_generation < data.feed_generation:
                        rejected.catalog_active = False
                        rejected.feed_generation = data.feed_generation
                        await session.flush()
                        await products.watches.evaluate(
                            session, rejected.product_id, market_country=rejected.market_country
                        )
                structlog.get_logger().warning(
                    "feed_item_rejected", program_id=str(program_id), code="identity_changed"
                )
                continue
            structlog.get_logger().info(
                "feed_item_materialized",
                network=program.network,
                program_id=str(program_id),
                market=program.market_country,
            )
        return len(ids)
