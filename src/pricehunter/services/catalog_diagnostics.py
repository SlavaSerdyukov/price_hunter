from dataclasses import asdict
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    PriceObservation,
    Product,
    ProductDiscovery,
    ProductIdentifier,
    ProductWatch,
    Store,
    StoreOffer,
    Tracker,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.products import ProductMatcher
from pricehunter.services.catalog_resolver import stored_evidence
from pricehunter.services.comparison_service import ComparisonReader


class CatalogDiagnostics:
    """Operator-only, bounded, read-only reports. Never mutate candidate products."""

    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self.sessions = sessions
        self.reader = ComparisonReader(settings)

    async def product(self, product_id: UUID, compare_with: UUID | None = None) -> dict[str, Any]:
        async with self.sessions() as session:
            product = await session.get(Product, product_id)
            if product is None:
                raise ProductNotFoundError()
            rows = (
                await session.execute(
                    select(StoreOffer, Store)
                    .join(Store)
                    .where(StoreOffer.product_id == product_id)
                    .order_by(Store.slug, StoreOffer.id)
                    .limit(50)
                )
            ).all()
            identifiers = list(
                await session.scalars(
                    select(ProductIdentifier)
                    .where(ProductIdentifier.product_id == product_id)
                    .order_by(ProductIdentifier.kind, ProductIdentifier.value)
                    .limit(200)
                )
            )
            reference = stored_evidence(product, *rows[0]) if rows else None
            if compare_with:
                other = await session.get(Product, compare_with)
                if other is None:
                    raise ProductNotFoundError()
                row = (
                    await session.execute(
                        select(StoreOffer, Store)
                        .join(Store)
                        .where(StoreOffer.product_id == compare_with)
                        .order_by(Store.slug, StoreOffer.id)
                        .limit(1)
                    )
                ).first()
                reference = stored_evidence(other, *row) if row else None
            counts = {}
            for name, model in (
                ("watches", ProductWatch),
                ("best_price_events", BestPriceEvent),
                ("discoveries", ProductDiscovery),
                ("offers", StoreOffer),
            ):
                counts[name] = await session.scalar(
                    select(func.count()).select_from(model).where(model.product_id == product_id)
                )
            offer_ids = select(StoreOffer.id).where(StoreOffer.product_id == product_id)
            counts["trackers"] = await session.scalar(
                select(func.count())
                .select_from(Tracker)
                .where(Tracker.store_offer_id.in_(offer_ids))
            )
            counts["observations"] = await session.scalar(
                select(func.count())
                .select_from(PriceObservation)
                .where(PriceObservation.store_offer_id.in_(offer_ids))
            )
            now = utcnow()
            return {
                "product_id": product.id,
                "canonical_name": product.canonical_name,
                "variant": product.variant,
                "identifiers": [
                    {
                        "kind": i.kind,
                        "value": i.value,
                        "source": i.source,
                        "confidence": i.confidence,
                    }
                    for i in identifiers
                ],
                "counts": counts,
                "offer_limit": 50,
                "identifier_limit": 200,
                "match_explanation": (
                    "Reconstructed against the first saved offer of compare_with or this product; "
                    "not an original decision audit."
                ),
                "offers": [
                    {
                        "offer_id": offer.id,
                        "store": store.name,
                        "country": store.country,
                        "saved_identity": offer.identity_data,
                        "confidence": offer.match_confidence,
                        "freshness": self.reader.view(offer, store, now).freshness,
                        "checked_at": offer.last_checked_at,
                        "match": asdict(
                            ProductMatcher().match(
                                reference, stored_evidence(product, offer, store)
                            )
                        )
                        if reference
                        else None,
                    }
                    for offer, store in rows
                ],
            }

    async def duplicates(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        ProductIdentifier.kind,
                        ProductIdentifier.value,
                        func.count(func.distinct(ProductIdentifier.product_id)).label("count"),
                    )
                    .where(ProductIdentifier.kind.in_(["gtin", "brand_model", "asin"]))
                    .group_by(ProductIdentifier.kind, ProductIdentifier.value)
                    .having(func.count(func.distinct(ProductIdentifier.product_id)) > 1)
                    .order_by(ProductIdentifier.kind, ProductIdentifier.value)
                    .limit(min(max(limit, 1), 100))
                )
            ).all()
            reports = []
            for kind, value, count in rows:
                ids = list(
                    await session.scalars(
                        select(ProductIdentifier.product_id)
                        .where(
                            ProductIdentifier.kind == kind,
                            ProductIdentifier.value == value,
                        )
                        .order_by(ProductIdentifier.product_id)
                        .limit(10)
                    )
                )
                reports.append(
                    {
                        "kind": kind,
                        "value": value,
                        "product_count": count,
                        "product_ids": ids,
                        "requires_review": True,
                        "sample_limit": 10,
                    }
                )
                structlog.get_logger().info(
                    "duplicate_candidate_found", identifier_kind=kind, product_count=count
                )
            return reports
