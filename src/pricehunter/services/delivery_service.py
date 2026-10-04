"""Explicit bounded quote acquisition; ordinary comparison never invokes providers."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.base import utcnow
from pricehunter.db.models import DeliveryQuote, Product, Store, StoreOffer
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import ComparisonProduct
from pricehunter.domain.delivery import (
    DeliveryContext,
    DeliveryOfferReference,
    DeliveryQuoteData,
    DeliveryStatus,
    delivered_total,
    snapshot_key,
)
from pricehunter.domain.discovery import Capability
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.freshness import Freshness
from pricehunter.domain.subscriptions import Feature
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.comparison_service import ComparisonReader, ComparisonService
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.market_context import user_market
from pricehunter.services.policy_resolver import PolicyResolver

log = structlog.get_logger()


class DeliveryService:
    async def purge_expired(self) -> int:
        async with self.sessions.begin() as session:
            ids = (
                select(DeliveryQuote.id)
                .where(DeliveryQuote.expires_at <= utcnow())
                .order_by(DeliveryQuote.expires_at, DeliveryQuote.id)
                .limit(self.settings.batch_size)
            )
            removed = await session.scalars(
                delete(DeliveryQuote).where(DeliveryQuote.id.in_(ids)).returning(DeliveryQuote.id)
            )
            return len(removed.all())

    def __init__(
        self,
        sessions: SessionFactory,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        settings: Settings,
        entitlements: EntitlementService,
    ) -> None:
        self.sessions, self.registry, self.limiter, self.settings, self.entitlements = (
            sessions,
            registry,
            limiter,
            settings,
            entitlements,
        )
        self.comparisons = ComparisonService(sessions, entitlements, settings)

    async def request(
        self,
        product_id: UUID,
        user_id: UUID,
        context: DeliveryContext,
        *,
        market_country: str | None = None,
    ) -> ComparisonProduct:
        # API/Telegram's shared authenticated middleware has consumed the user budget.
        (await self.entitlements.for_user(user_id)).entitlements.require(Feature.COMPARISON_SEARCH)
        now = utcnow()
        capable = [
            p.name
            for p in self.registry.providers.values()
            if Capability.DELIVERY_QUOTE in p.capabilities
        ]
        async with self.sessions() as session:
            if await session.get(Product, product_id) is None:
                raise ProductNotFoundError()
            market = await user_market(session, user_id, market_country)
            reader = ComparisonReader(
                self.settings, permission="refresh_allowed", market_country=market
            )
            ranked = (
                select(
                    StoreOffer.id,
                    StoreOffer.currency,
                    StoreOffer.price,
                    Store.merchant_id,
                    func.row_number()
                    .over(
                        partition_by=Store.merchant_id,
                        order_by=(StoreOffer.currency, StoreOffer.price, StoreOffer.id),
                    )
                    .label("merchant_round"),
                )
                .join(Store)
                .where(
                    *reader.filters(product_id),
                    Store.provider_type.in_(capable),
                    reader.freshness_expression(now) == Freshness.FRESH,
                    StoreOffer.availability == "in_stock",
                )
                .cte("delivery_quote_candidates")
            )
            rows = (
                await session.execute(
                    select(StoreOffer, Store)
                    .join(Store)
                    .join(ranked, ranked.c.id == StoreOffer.id)
                    .order_by(
                        ranked.c.merchant_round,
                        ranked.c.currency,
                        ranked.c.price,
                        ranked.c.merchant_id,
                        ranked.c.id,
                    )
                    .limit(self.settings.delivery_quote_limit)
                )
            ).all()
            cached = list(
                await session.scalars(
                    select(DeliveryQuote)
                    .where(
                        DeliveryQuote.offer_id.in_([o.id for o, _ in rows]),
                        DeliveryQuote.destination_key.in_(
                            [context.fingerprint, context.country_key]
                        ),
                        DeliveryQuote.country == context.country,
                    )
                    .order_by(
                        case((DeliveryQuote.scope == "exact", 0), else_=1),
                        DeliveryQuote.quoted_at.desc(),
                    )
                )
            )
            work: list[tuple[DeliveryOfferReference, str, int, bool]] = []
            for offer, store in rows:
                policy = PolicyResolver(self.settings).offer(offer, store)
                ref = DeliveryOfferReference(
                    offer_id=offer.id,
                    snapshot_key=snapshot_key(
                        offer.price, offer.currency, offer.external_id, offer.url
                    ),
                    external_id=offer.external_id,
                    store_slug=store.slug,
                    market_country=offer.market_country,
                    currency=offer.currency,
                    price=offer.price,
                    url=offer.url,
                )
                ttl = min(self.settings.delivery_quote_ttl_seconds, policy.max_cache_seconds)
                if any(
                    q.offer_id == offer.id
                    and q.snapshot_key == ref.snapshot_key
                    and q.currency == offer.currency
                    and q.quoted_at <= now < min(q.expires_at, q.quoted_at + timedelta(seconds=ttl))
                    for q in cached
                ):
                    continue
                work.append(
                    (
                        ref,
                        store.provider_type,
                        ttl,
                        policy.catalog_persistence_allowed
                        and self.registry.get(store.provider_type).delivery_quote_cacheable,
                    )
                )
        semaphore = asyncio.Semaphore(self.settings.delivery_quote_concurrency)
        results: list[tuple[dict[str, object], bool]] = []

        async def acquire(
            ref: DeliveryOfferReference, provider_name: str, ttl: int, cacheable: bool
        ) -> None:
            started = utcnow()
            quote: DeliveryQuoteData | None = None
            status = DeliveryStatus.UNSUPPORTED
            try:
                async with semaphore, asyncio.timeout(self.settings.delivery_quote_timeout_seconds):
                    async with self.limiter.offer_refresh(ref.offer_id) as claimed:
                        if not claimed:
                            status = DeliveryStatus.FAILED
                        else:
                            async with self.limiter.provider(provider_name):
                                quote = await self.registry.get(provider_name).quote_delivery(
                                    ref, context
                                )
                            if quote is not None:
                                quote = DeliveryQuoteData.model_validate(
                                    quote.model_dump(warnings=False)
                                )
                                if (
                                    (
                                        quote.offer_id,
                                        quote.snapshot_key,
                                        quote.currency,
                                        quote.source,
                                    )
                                    != (ref.offer_id, ref.snapshot_key, ref.currency, provider_name)
                                    or not quote.matches(context)
                                    or not quote.current(utcnow())
                                ):
                                    raise ValueError("Incompatible quote")
                                status = quote.status
            except Exception:
                # Provider exception text/responses and destination never reach logs.
                quote, status = None, DeliveryStatus.FAILED
            if quote is None:
                quote = DeliveryQuoteData(
                    offer_id=ref.offer_id,
                    snapshot_key=ref.snapshot_key,
                    country=context.country,
                    scope="exact",
                    destination_key=context.fingerprint,
                    currency=ref.currency,
                    quoted_at=started,
                    expires_at=started + timedelta(seconds=min(30, ttl)),
                    source=provider_name,
                    status=status,
                )
            expiry = min(quote.expires_at, quote.quoted_at + timedelta(seconds=ttl))
            total = delivered_total(ref.price, ref.currency, quote)
            if status not in (DeliveryStatus.FAILED, DeliveryStatus.UNSUPPORTED):
                status = (
                    DeliveryStatus.COMPLETE
                    if total is not None and quote.availability == "in_stock"
                    else DeliveryStatus.INCOMPLETE
                )
            values: dict[str, object] = quote.model_dump() | {
                "id": uuid4(),
                "expires_at": expiry,
                "status": status,
                "item_price": ref.price,
                "offer_url": ref.url,
                "delivered_total": total,
            }
            results.append((values, cacheable))
            log.info(
                "delivery_quote_result",
                provider=provider_name,
                offer_id=str(ref.offer_id),
                market_country=ref.market_country,
                quote_status=str(status),
            )

        tasks = [asyncio.create_task(acquire(*item)) for item in work]
        try:
            async with asyncio.timeout(self.settings.delivery_operation_timeout_seconds):
                await asyncio.gather(*tasks)
        except TimeoutError:
            # Cancellation stops remote requests; no product-price failure is recorded.
            pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Every unscheduled/timed-out candidate receives request-scoped FAILED evidence.
        completed = {row["offer_id"] for row, _ in results}
        for ref, name, ttl, _ in work:
            if ref.offer_id not in completed:
                failure = DeliveryQuoteData(
                    offer_id=ref.offer_id,
                    snapshot_key=ref.snapshot_key,
                    country=context.country,
                    scope="exact",
                    destination_key=context.fingerprint,
                    currency=ref.currency,
                    quoted_at=now,
                    expires_at=now + timedelta(seconds=min(30, ttl)),
                    source=name,
                    status=DeliveryStatus.FAILED,
                )
                results.append(
                    (
                        failure.model_dump()
                        | {
                            "id": uuid4(),
                            "item_price": ref.price,
                            "offer_url": ref.url,
                            "delivered_total": None,
                        },
                        False,
                    )
                )
        ephemeral = []
        if results:
            async with self.sessions.begin() as session:
                # Batch revalidation: concurrent item/policy changes never gain old evidence.
                current = (
                    await session.execute(
                        select(StoreOffer, Store)
                        .join(Store)
                        .where(
                            *reader.filters(product_id),
                            StoreOffer.id.in_([r["offer_id"] for r, _ in results]),
                        )
                        .with_for_update(of=StoreOffer)
                    )
                ).all()
                offers = {o.id: (o, store) for o, store in current}
                persisted = []
                for row, cacheable in results:
                    current_offer = offers.get(row["offer_id"])
                    if current_offer is None:
                        continue
                    offer, store = current_offer
                    if row["snapshot_key"] != snapshot_key(
                        offer.price, offer.currency, offer.external_id, offer.url
                    ):
                        continue
                    policy = PolicyResolver(self.settings).offer(offer, store)
                    if cacheable and policy.catalog_persistence_allowed:
                        persisted.append(row)
                    else:
                        ephemeral.append(row)
                if persisted:
                    statement = insert(DeliveryQuote).values(persisted)
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[
                                DeliveryQuote.offer_id,
                                DeliveryQuote.destination_key,
                                DeliveryQuote.scope,
                                DeliveryQuote.currency,
                            ],
                            set_={
                                c.name: getattr(statement.excluded, c.name)
                                for c in DeliveryQuote.__table__.columns
                                if c.name
                                not in {"id", "offer_id", "destination_key", "scope", "currency"}
                            },
                            where=statement.excluded.quoted_at >= DeliveryQuote.quoted_at,
                        )
                    )
        return await self.comparisons.get(
            product_id,
            user_id,
            market_country=market,
            delivery_context=context,
            delivery_quotes=ephemeral,
        )
