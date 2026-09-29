import asyncio
from contextlib import nullcontext
from uuid import UUID

import structlog
from pydantic import BaseModel

from pricehunter.core.limits import RateLimiter
from pricehunter.domain.comparison import ComparisonProduct, comparison_rank
from pricehunter.domain.ingestion import search_ingestion
from pricehunter.domain.products import ProductOfferData
from pricehunter.domain.subscriptions import Feature
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.market_context import user_market
from pricehunter.services.product_service import ProductService


class SearchResult(BaseModel):
    products: list[ComparisonProduct]
    unavailable_providers: list[str]


class SearchService:
    def __init__(
        self,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        entitlements: EntitlementService,
        products: ProductService,
    ) -> None:
        self.registry, self.limiter, self.entitlements = registry, limiter, entitlements
        self.products = products

    async def search(
        self,
        query: str,
        user_id: UUID,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> SearchResult:
        async with self.products.sessions() as session:
            country = await user_market(session, user_id, country)
        await self.limiter.user(user_id)
        limits = (await self.entitlements.for_user(user_id)).entitlements
        limits.require(Feature.COMPARISON_SEARCH)
        await self.limiter.check(f"search:{user_id}", limit=limits.search_limit, seconds=86400)

        async def one(provider: StoreProvider) -> tuple[list[ProductOfferData], str | None]:
            try:
                async with (
                    asyncio.timeout(self.limiter.settings.provider_timeout_seconds),
                    (
                        nullcontext()
                        if provider.manages_request_limits
                        else self.limiter.provider(provider.name)
                    ),
                ):
                    return await provider.search(query, country=country, currency=currency), None
            except Exception:
                structlog.get_logger().warning("search_provider_failed", provider=provider.name)
                return [], provider.name

        responses = await asyncio.gather(*(one(p) for p in self.registry.providers.values()))
        persisted: set[UUID] = set()
        incoming = sorted(
            (offer for offers, _ in responses for offer in offers[:100]),
            key=lambda offer: (offer.store_slug, offer.external_id),
        )
        unavailable = {name for _, name in responses if name}
        for data in incoming:
            if data.country != country:
                continue
            try:
                persisted.add(
                    (
                        await self.products.persist(
                            data,
                            mode=search_ingestion(self.registry.get(data.provider).capabilities),
                        )
                    ).product_id
                )
            except Exception:
                unavailable.add(data.provider)
                structlog.get_logger().warning("search_persistence_failed", provider=data.provider)
        comparisons = [
            await self.products.product(product_id, user_id, market_country=country)
            for product_id in persisted
        ]
        comparisons.sort(key=lambda product: comparison_rank(product, query))
        return SearchResult(
            products=comparisons[: limits.search_result_limit],
            unavailable_providers=sorted(unavailable),
        )
