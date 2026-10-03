from sqlalchemy import and_, delete, exists, or_, select

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    Product,
    ProductBestState,
    ProductDiscovery,
    ProductIdentifier,
    ProductWatch,
    Store,
    StoreOffer,
    Tracker,
)
from pricehunter.db.session import SessionFactory
from pricehunter.services.policy_resolver import PolicyResolver


class CatalogPolicyMaintenance:
    """Bounded eviction for reviewed, search-only catalog caches.

    Never delete pre-existing user tracking/history when an operator changes a
    contract: that requires a separate retention/export decision during review.
    New search-only providers cannot create these protected references.
    """

    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    async def purge(self) -> int:
        rules = [
            PolicyResolver(self.settings).allowed("catalog_persistence_allowed"),
            ~PolicyResolver(self.settings).allowed("price_history_allowed"),
            ~PolicyResolver(self.settings).allowed(
                "catalog_persistence_allowed", now=utcnow(), current_feed=False
            ),
        ]
        protected = [
            exists().where(Tracker.store_offer_id == StoreOffer.id),
            exists().where(ProductWatch.best_offer_id == StoreOffer.id),
            exists().where(ProductBestState.store_offer_id == StoreOffer.id),
            exists().where(BestPriceEvent.store_offer_id == StoreOffer.id),
        ]
        count = 0
        async with self.sessions.begin() as session:
            candidates = list(
                (
                    await session.execute(
                        select(StoreOffer.id, StoreOffer.product_id, Store.slug)
                        .join(Store)
                        .where(and_(*rules), ~or_(*protected))
                        .order_by(StoreOffer.product_id, StoreOffer.id)
                        .limit(self.settings.batch_size)
                    )
                ).all()
            )
            for offer_id, product_id, source in candidates:
                await session.get(Product, product_id, with_for_update=True)
                deleted = await session.scalar(
                    delete(StoreOffer)
                    .where(
                        StoreOffer.id == offer_id,
                        ~or_(*protected),
                    )
                    .returning(StoreOffer.id)
                )
                if deleted is None:
                    continue
                count += 1
                # Keep identifiers supplied by other listings of this same merchant.
                same_source = await session.scalar(
                    select(StoreOffer.id)
                    .join(Store)
                    .where(
                        StoreOffer.product_id == product_id,
                        Store.slug == source,
                    )
                    .limit(1)
                )
                if same_source is None:
                    await session.execute(
                        delete(ProductIdentifier).where(
                            ProductIdentifier.product_id == product_id,
                            ProductIdentifier.source == source,
                        )
                    )
                await session.execute(
                    delete(Product).where(
                        Product.id == product_id,
                        ~exists().where(StoreOffer.product_id == Product.id),
                        ~exists().where(ProductWatch.product_id == Product.id),
                        ~exists().where(ProductDiscovery.product_id == Product.id),
                        ~exists().where(ProductBestState.product_id == Product.id),
                        ~exists().where(BestPriceEvent.product_id == Product.id),
                    )
                )
        return count
