import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.selectable import Subquery

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.base import utcnow
from pricehunter.db.models import Product, ProductDiscovery, ProductIdentifier, ProductWatch, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.discovery import (
    Capability,
    DiscoveryMismatch,
    DiscoveryQuery,
    ProductSearchIdentity,
)
from pricehunter.domain.errors import ProductNotFoundError, RateLimitExceededError
from pricehunter.domain.products import ProductOfferData
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.catalog_resolver import CatalogResolver
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.product_watch_service import ProductWatchService

log = structlog.get_logger()


@dataclass(frozen=True)
class DiscoveryClaim:
    target_id: UUID
    token: UUID


class ExpiredDiscovery(Exception):
    pass


class ProductDiscoveryService:
    def __init__(
        self,
        sessions: SessionFactory,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        settings: Settings,
        entitlements: EntitlementService,
    ) -> None:
        self.sessions, self.registry, self.limiter = sessions, registry, limiter
        self.settings, self.entitlements = settings, entitlements
        self.resolver = CatalogResolver()
        self.watches = ProductWatchService(sessions, entitlements, settings)

    def interests(self, now: datetime) -> Subquery:
        plan = self.entitlements.plan_expression(now, ProductWatch)
        interval = case(
            *[(plan == p, seconds) for p, seconds in self.settings.discovery_plan_seconds.items()],
            else_=self.settings.discovery_plan_seconds["free"],
        )
        country = func.coalesce(User.country_code, "BE")
        return (
            select(
                ProductWatch.product_id,
                country.label("country"),
                ProductWatch.currency,
                func.min(interval).label("interval"),
            )
            .join(User)
            .where(ProductWatch.id.in_(self.entitlements.scheduled_watch_ids(now)))
            .group_by(ProductWatch.product_id, country, ProductWatch.currency)
            .subquery()
        )

    async def synchronize(self) -> int:
        if not self.settings.discovery_enabled:
            return 0
        count = 0
        now = utcnow()
        async with self.sessions.begin() as session:
            interests = self.interests(now)
            for provider in self.registry.providers.values():
                if not provider.capabilities & {
                    Capability.SEARCH_GTIN,
                    Capability.SEARCH_MODEL,
                    Capability.SEARCH_ASIN,
                }:
                    continue
                missing = (
                    select(interests)
                    .where(
                        ~exists().where(
                            ProductDiscovery.product_id == interests.c.product_id,
                            ProductDiscovery.country == interests.c.country,
                            ProductDiscovery.currency == interests.c.currency,
                            ProductDiscovery.provider == provider.name,
                        )
                    )
                    .order_by(interests.c.product_id, interests.c.country, interests.c.currency)
                    .limit(self.settings.discovery_batch_size)
                )
                if provider.discovery_countries:
                    missing = missing.where(interests.c.country.in_(provider.discovery_countries))
                for row in (await session.execute(missing)).all():
                    inserted = await session.scalar(
                        insert(ProductDiscovery)
                        .values(
                            id=uuid4(),
                            product_id=row.product_id,
                            provider=provider.name,
                            country=row.country,
                            currency=row.currency,
                            next_discovery_at=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                ProductDiscovery.product_id,
                                ProductDiscovery.provider,
                                ProductDiscovery.country,
                                ProductDiscovery.currency,
                            ]
                        )
                        .returning(ProductDiscovery.id)
                    )
                    count += inserted is not None
        return count

    async def claim_due(self) -> list[DiscoveryClaim]:
        if not self.settings.discovery_enabled or not self.registry.providers:
            return []
        now = utcnow()
        interests = self.interests(now)
        overrides = [
            (ProductDiscovery.provider == p.name, p.discovery_interval_seconds)
            for p in self.registry.providers.values()
            if p.discovery_interval_seconds
        ]
        interval = (
            case(*overrides, else_=interests.c.interval) if overrides else interests.c.interval
        )
        elapsed = ProductDiscovery.last_success_at <= now - func.make_interval(
            0, 0, 0, 0, 0, 0, interval
        )
        async with self.sessions.begin() as session:
            targets = list(
                await session.scalars(
                    select(ProductDiscovery)
                    .join(
                        interests,
                        and_(
                            ProductDiscovery.product_id == interests.c.product_id,
                            ProductDiscovery.country == interests.c.country,
                            ProductDiscovery.currency == interests.c.currency,
                        ),
                    )
                    .where(
                        ProductDiscovery.provider.in_(self.registry.providers),
                        or_(
                            ProductDiscovery.lease_until.is_(None),
                            ProductDiscovery.lease_until < now,
                        ),
                        or_(
                            ProductDiscovery.next_discovery_at <= now,
                            and_(
                                ProductDiscovery.failure_count == 0,
                                ProductDiscovery.last_error_code.is_(None),
                                elapsed,
                            ),
                        ),
                        or_(
                            ProductDiscovery.last_success_at.is_(None),
                            ProductDiscovery.failure_count > 0,
                            elapsed,
                        ),
                    )
                    .order_by(ProductDiscovery.next_discovery_at, ProductDiscovery.id)
                    .limit(self.settings.discovery_batch_size)
                    .with_for_update(of=ProductDiscovery, skip_locked=True)
                )
            )
            claims = []
            for target in targets:
                target.lease_token = uuid4()
                target.lease_until = now + timedelta(seconds=self.settings.discovery_lease_seconds)
                claims.append(DiscoveryClaim(target.id, target.lease_token))
            return claims

    async def discover(self, claim: DiscoveryClaim) -> bool:
        async with self.limiter.offer_refresh(f"discovery:{claim.target_id}") as acquired:
            if not acquired:
                return False
            return await self._discover(claim)

    async def _discover(self, claim: DiscoveryClaim) -> bool:
        if not self.settings.discovery_enabled:
            return False
        async with self.sessions() as session:
            target = await session.scalar(
                select(ProductDiscovery).where(
                    ProductDiscovery.id == claim.target_id,
                    ProductDiscovery.lease_token == claim.token,
                    ProductDiscovery.lease_until > utcnow(),
                )
            )
            if target is None or target.provider not in self.registry.providers:
                return False
            interests = self.interests(utcnow())
            cadence = await session.scalar(
                select(interests.c.interval).where(
                    interests.c.product_id == target.product_id,
                    interests.c.country == target.country,
                    interests.c.currency == target.currency,
                )
            )
            if cadence is None:
                return False
            product = await session.get(Product, target.product_id)
            assert product is not None
            asin = await session.scalar(
                select(ProductIdentifier.value)
                .where(
                    ProductIdentifier.product_id == product.id,
                    ProductIdentifier.kind == "asin",
                )
                .limit(1)
            )
            provider = self.registry.get(target.provider)
            query = ProductSearchIdentity(
                product.gtin,
                product.brand,
                product.model,
                product.mpn,
                asin.upper() if asin else None,
            ).query(provider.capabilities, target.country, target.currency)
            interval = provider.discovery_interval_seconds or int(cadence)
        if query is None:
            await self.failed(claim, "insufficient_identity", interval)
            return False
        circuit = f"ph:discovery:circuit:{provider.name}"
        if await self.limiter.redis.exists(circuit):
            await self.failed(
                claim, "provider_suppressed", self.settings.discovery_suppression_seconds
            )
            return False
        log.info(
            "discovery_started",
            product_id=str(target.product_id),
            provider=provider.name,
            method=query.method,
        )
        try:
            # A hard timeout also covers fixture/custom adapters exempt from ordinary mock limits.
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                async with self.limiter.provider(provider.name):
                    results = await provider.discover(query)
                if Capability.SEARCH_DETAILS in provider.capabilities:
                    detailed = []
                    for offer in results[: self.settings.discovery_result_limit]:
                        # Summary identifiers are not inferred from the search query.
                        # Each details operation consumes the shared provider budget.
                        async with self.limiter.provider(provider.name):
                            try:
                                detailed.append(await provider.discovery_details(offer))
                            except ProductNotFoundError:
                                continue  # Listing ended between search and details.
                    results = detailed
        except Exception as exc:
            code = (
                "rate_limited"
                if isinstance(exc, RateLimitExceededError)
                else "provider_unavailable"
            )
            await self.failed(claim, code)
            if code != "rate_limited":
                key = f"ph:discovery:failures:{provider.name}"
                failures = await self.limiter.redis.incr(key)
                await self.limiter.redis.expire(key, self.settings.discovery_suppression_seconds)
                if failures >= self.settings.discovery_failure_threshold:
                    await self.limiter.redis.set(
                        circuit, "1", ex=self.settings.discovery_suppression_seconds
                    )
            log.warning(
                "discovery_provider_failed",
                product_id=str(target.product_id),
                provider=provider.name,
                error_code=code,
            )
            return False
        try:
            accepted = await self.accept(claim, results, query, interval)
        except Exception:
            await self.failed(claim, "persistence_failed")
            log.warning(
                "discovery_provider_failed",
                product_id=str(target.product_id),
                provider=provider.name,
                error_code="persistence_failed",
            )
            return False
        if accepted:
            await self.limiter.redis.delete(f"ph:discovery:failures:{provider.name}")
        return accepted

    async def accept(
        self,
        claim: DiscoveryClaim,
        results: list[ProductOfferData],
        query: DiscoveryQuery,
        interval: int,
    ) -> bool:
        try:
            async with self.sessions.begin() as session:
                target = await session.scalar(
                    select(ProductDiscovery)
                    .where(
                        ProductDiscovery.id == claim.target_id,
                        ProductDiscovery.lease_token == claim.token,
                    )
                    .with_for_update()
                )
                if target is None or target.lease_until is None or target.lease_until <= utcnow():
                    return False
                if (query.country, query.currency) != (target.country, target.currency):
                    raise ValueError("Discovery context changed")
                interests = self.interests(utcnow())
                if (
                    await session.scalar(
                        select(interests.c.product_id).where(
                            interests.c.product_id == target.product_id,
                            interests.c.country == target.country,
                            interests.c.currency == target.currency,
                        )
                    )
                    is None
                ):
                    target.lease_until, target.lease_token = None, None
                    return False
                incoming = sorted(
                    results[: self.settings.discovery_result_limit],
                    key=lambda d: (d.store_slug, d.external_id),
                )
                await self.resolver.lock_evidence(session, incoming)
                found: set[UUID] = set()
                for data in incoming:
                    if data.currency != target.currency or data.provider != target.provider:
                        continue
                    try:
                        async with session.begin_nested():
                            offer = await self.resolver.resolve(
                                session, data, expected_product_id=target.product_id
                            )
                            found.add(offer.id)
                            log.info(
                                "discovery_offer_found",
                                product_id=str(target.product_id),
                                provider=target.provider,
                                offer_id=str(offer.id),
                            )
                    except DiscoveryMismatch:
                        continue
                await session.get(Product, target.product_id, with_for_update=True)
                await session.flush()
                await self.watches.evaluate(session, target.product_id)
                if target.lease_until <= utcnow():
                    raise ExpiredDiscovery()
                now = utcnow()
                target.last_discovered_at = target.last_success_at = now
                target.next_discovery_at = now + timedelta(seconds=interval)
                target.failure_count, target.last_error_code = 0, None
                target.lease_token, target.lease_until = None, None
                target.sequence += 1
                target.result_count = len(found)
                log.info(
                    "discovery_completed",
                    product_id=str(target.product_id),
                    provider=target.provider,
                    offer_count=len(found),
                )
                return True
        except ExpiredDiscovery:
            return False

    async def failed(self, claim: DiscoveryClaim, code: str, delay: int | None = None) -> None:
        async with self.sessions.begin() as session:
            target = await session.scalar(
                select(ProductDiscovery)
                .where(
                    ProductDiscovery.id == claim.target_id,
                    ProductDiscovery.lease_token == claim.token,
                    ProductDiscovery.lease_until > utcnow(),
                )
                .with_for_update()
            )
            if target is None:
                return
            target.failure_count += 1
            wait = delay or min(86400, 60 * 2 ** min(target.failure_count, 10)) + random.randint(
                0, 30
            )
            target.last_error_code, target.last_discovered_at = code, utcnow()
            target.next_discovery_at = utcnow() + timedelta(seconds=wait)
            target.lease_token, target.lease_until = None, None
