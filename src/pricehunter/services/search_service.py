import asyncio
from uuid import UUID

import structlog
from pydantic import BaseModel

from pricehunter.core.limits import RateLimiter
from pricehunter.domain.products import ProductMatcher, ProductOfferData
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.registry import ProviderRegistry


class SearchResult(BaseModel):
    groups: list[list[ProductOfferData]]
    unavailable_providers: list[str]


class SearchService:
    def __init__(self, registry: ProviderRegistry, limiter: RateLimiter) -> None:
        self.registry, self.limiter = registry, limiter

    async def search(
        self,
        query: str,
        user_id: UUID,
        *,
        country: str | None = None,
        currency: str | None = None,
    ) -> SearchResult:
        await self.limiter.user(user_id)

        async def one(provider: StoreProvider) -> tuple[list[ProductOfferData], str | None]:
            try:
                async with self.limiter.provider(provider.name):
                    return await provider.search(query, country=country, currency=currency), None
            except Exception:
                structlog.get_logger().warning("search_provider_failed", provider=provider.name)
                return [], provider.name

        responses = await asyncio.gather(*(one(p) for p in self.registry.providers.values()))
        groups: list[list[ProductOfferData]] = []
        matcher = ProductMatcher()
        for offers, _ in responses:
            for offer in offers:
                for group in groups:
                    if matcher.match(offer, group[0]).matched:
                        group.append(offer)
                        break
                else:
                    groups.append([offer])
        for group in groups:
            group.sort(key=lambda x: (x.currency, x.price))
        return SearchResult(groups=groups, unavailable_providers=[p for _, p in responses if p])
