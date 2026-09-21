import hashlib
from collections.abc import Iterable
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import exists, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.models import Product, ProductIdentifier, Store, StoreOffer
from pricehunter.db.repositories.catalog import CatalogRepository
from pricehunter.domain.discovery import DiscoveryMismatch
from pricehunter.domain.products import (
    ProductMatcher,
    ProductOfferData,
    identity_key,
    identity_signals,
    normalized_variant,
    trade_id,
)


def stored_evidence(product: Product, offer: StoreOffer, store: Store) -> ProductOfferData:
    identity = offer.identity_data or {
        key: getattr(product, key)
        for key in ("brand", "model", "mpn", "gtin", "ean", "upc", "asin", "variant")
    }
    return ProductOfferData(
        provider=store.provider_type,
        store_slug=store.slug,
        store_name=store.name,
        store_domain=store.domain,
        country=store.country,
        external_id=offer.external_id,
        url=offer.url,
        title=offer.title,
        price=offer.price,
        currency=offer.currency,
        availability=offer.availability,
        **identity,
    )


class CatalogResolver:
    """One persistence/matching path for URL resolution and provider search results."""

    @staticmethod
    def _confidence(data: ProductOfferData) -> Decimal:
        kinds = {kind for kind, _ in identity_signals(data)}
        if trade_id(data):
            return Decimal(1)
        if "asin" in kinds:
            return Decimal("0.98")
        return Decimal("0.95") if "brand_model" in kinds else Decimal(0)

    async def _index(self, session: AsyncSession, product_id: UUID, data: ProductOfferData) -> None:
        for kind, value in identity_signals(data):
            await session.execute(
                insert(ProductIdentifier)
                .values(
                    id=uuid4(),
                    product_id=product_id,
                    kind=kind,
                    value=value,
                    source=data.store_slug,
                    confidence={"brand_model": Decimal("0.95"), "asin": Decimal("0.98")}.get(
                        kind, Decimal(1)
                    ),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ProductIdentifier.product_id,
                        ProductIdentifier.kind,
                        ProductIdentifier.value,
                    ]
                )
            )

    async def lock_evidence(
        self, session: AsyncSession, offers: Iterable[ProductOfferData]
    ) -> None:
        signals = sorted({signal for offer in offers for signal in identity_signals(offer)})
        for kind, value in signals:
            digest = hashlib.sha256(f"catalog:{kind}:{value}".encode()).digest()
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(digest[:8], "big", signed=True)},
            )

    async def resolve(
        self,
        session: AsyncSession,
        data: ProductOfferData,
        *,
        expected_product_id: UUID | None = None,
    ) -> StoreOffer:
        signals = identity_signals(data)
        await self.lock_evidence(session, [data])
        existing = await session.scalar(
            select(StoreOffer)
            .join(Store)
            .where(
                Store.slug == data.store_slug,
                StoreOffer.external_id == data.external_id,
                Store.active.is_(True),
                Store.supported.is_(True),
            )
        )
        if existing:
            if expected_product_id and existing.product_id != expected_product_id:
                raise DiscoveryMismatch()
            # Preserve the historical listing's identity and price. Refreshes own prices.
            return existing
        candidates = list(
            await session.scalars(
                select(Product)
                .where(
                    Product.id.in_(
                        select(ProductIdentifier.product_id).where(
                            tuple_(ProductIdentifier.kind, ProductIdentifier.value).in_(signals)
                        )
                    )
                )
                .order_by(Product.id)
                .with_for_update()
            )
        )
        matches: list[tuple[Product, Decimal]] = []
        matcher = ProductMatcher()
        for candidate in candidates:
            rows = (
                await session.execute(
                    select(StoreOffer, Store)
                    .join(Store)
                    .where(StoreOffer.product_id == candidate.id)
                )
            ).all()
            results = [
                matcher.match(data, stored_evidence(candidate, offer, store))
                for offer, store in rows
            ]
            # Do not let a weak listing bridge two conflicting strong identities.
            for result in results:
                if result.method in ("identifier_conflict", "variant_mismatch"):
                    structlog.get_logger().info(
                        "catalog_match_rejected",
                        product_id=str(candidate.id),
                        store=data.store_slug,
                        method=result.method,
                        reasons=result.reasons,
                    )
            if results and all(result.matched for result in results):
                matches.append((candidate, max(result.confidence for result in results)))
        if matches:
            highest = max(confidence for _, confidence in matches)
            matches = [
                (product, confidence) for product, confidence in matches if confidence == highest
            ]
        if expected_product_id and (len(matches) != 1 or matches[0][0].id != expected_product_id):
            raise DiscoveryMismatch()
        if len(matches) == 1:
            product, confidence = matches[0]
            for key in ("brand", "model", "mpn", "ean", "upc", "asin"):
                if not getattr(product, key) and getattr(data, key):
                    setattr(product, key, getattr(data, key))
            if not product.gtin:
                product.gtin = trade_id(data)
        else:
            # Ambiguous candidates stay separate; stable keys never depend on a title.
            key = identity_key(data)
            if await session.scalar(select(Product.id).where(Product.identity_key == key)):
                key = hashlib.sha256(
                    f"{key}:{data.store_slug}:{data.external_id}".encode()
                ).hexdigest()
            product = Product(
                id=uuid4(),
                identity_key=key,
                canonical_name=data.title,
                brand=data.brand,
                model=data.model,
                mpn=data.mpn,
                gtin=trade_id(data),
                ean=data.ean,
                upc=data.upc,
                asin=data.asin,
                variant=normalized_variant(data.variant),
            )
            session.add(product)
            await session.flush()
            confidence = self._confidence(data)
        await self._index(session, product.id, data)
        offer = await CatalogRepository(session).save_resolved(data, product.id, confidence)
        structlog.get_logger().info(
            "canonical_offer_added",
            product_id=str(product.id),
            offer_id=str(offer.id),
            store=data.store_slug,
            match_confidence=str(confidence),
            candidate_count=len(candidates),
        )
        return offer

    async def backfill(self, session: AsyncSession, limit: int = 200) -> int:
        """Index legacy products without changing IDs, offers, trackers or history."""
        products = list(
            await session.scalars(
                select(Product)
                .where(~exists().where(ProductIdentifier.product_id == Product.id))
                .order_by(Product.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for product in products:
            rows = (
                await session.execute(
                    select(StoreOffer, Store).join(Store).where(StoreOffer.product_id == product.id)
                )
            ).all()
            for offer, store in rows:
                data = stored_evidence(product, offer, store)
                offer.match_confidence = self._confidence(data)
                offer.identity_data = data.model_dump(
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
                )
                await self._index(session, product.id, data)
            if not rows:
                session.add(
                    ProductIdentifier(
                        product_id=product.id,
                        kind="legacy",
                        value=str(product.id),
                        source="migration",
                        confidence=0,
                    )
                )
        return len(products)
