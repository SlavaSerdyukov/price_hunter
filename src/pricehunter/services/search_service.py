import asyncio
from contextlib import nullcontext
from itertools import zip_longest
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from pricehunter.core.limits import RateLimiter
from pricehunter.domain.comparison import ComparisonProduct, comparison_rank
from pricehunter.domain.ingestion import search_ingestion
from pricehunter.domain.products import ProductOfferData, normalized_variant
from pricehunter.domain.search import ProviderSearchOutcome
from pricehunter.domain.subscriptions import Feature
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.market_context import user_market
from pricehunter.services.product_service import ProductService
from pricehunter.services.provider_health import ProviderHealth


class SearchResult(BaseModel):
    products: list[ComparisonProduct]
    unavailable_providers: list[str]
    provider_outcomes: list[ProviderSearchOutcome] = Field(default_factory=list, exclude=True)


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
        self.health = ProviderHealth(limiter.redis)

    async def search(
        self, query: str, user_id: UUID, *, country: str | None = None, currency: str | None = None
    ) -> SearchResult:
        async with self.products.sessions() as session:
            country = await user_market(session, user_id, country)
        await self.limiter.user(user_id)
        limits = (await self.entitlements.for_user(user_id)).entitlements
        limits.require(Feature.COMPARISON_SEARCH)
        await self.limiter.check(f"search:{user_id}", limit=limits.search_limit, seconds=86400)
        settings = self.limiter.settings

        def error(outcome: ProviderSearchOutcome, code: str) -> None:
            if code not in outcome.errors and len(outcome.errors) < settings.search_error_limit:
                outcome.errors.append(code)

        def content_version(data: ProductOfferData) -> tuple[float, str]:
            # Normalize timezone offsets and exclude all commercial/link metadata
            # from tie-breaking, just as matching and comparison ranking do.
            return (
                data.source_updated_at.timestamp() if data.source_updated_at else float("-inf"),
                data.model_dump_json(
                    include={
                        "title",
                        "price",
                        "currency",
                        "availability",
                        "original_price",
                        "brand",
                        "model",
                        "mpn",
                        "gtin",
                        "ean",
                        "upc",
                        "asin",
                        "sku",
                        "variant",
                    }
                ),
            )

        async def one(
            provider: StoreProvider,
        ) -> tuple[StoreProvider, list[ProductOfferData], ProviderSearchOutcome]:
            outcome = ProviderSearchOutcome(provider=provider.name)
            try:
                async with (
                    asyncio.timeout(settings.provider_timeout_seconds),
                    (
                        nullcontext()
                        if provider.manages_request_limits
                        else self.limiter.provider(provider.name)
                    ),
                ):
                    results = await provider.search(query, country=country, currency=currency)
                if not isinstance(results, list):
                    raise ValueError("Invalid provider result envelope")
                outcome.result_count = len(results)
                if len(results) > settings.search_provider_candidate_limit:
                    error(outcome, "candidate_limit")
                candidates: dict[
                    tuple[str, str, str, str, tuple[tuple[str, str], ...]], ProductOfferData
                ] = {}
                for data in results[: settings.search_provider_candidate_limit]:
                    if not isinstance(data, ProductOfferData) or data.provider != provider.name:
                        outcome.rejected_count += 1
                        error(outcome, "invalid_item")
                        continue
                    identity = (
                        data.provider,
                        data.store_slug,
                        data.external_id,
                        data.country,
                        tuple(sorted(normalized_variant(data.variant).items())),
                    )
                    # Pick a deterministic version without collapsing variants or markets.
                    if identity in candidates:
                        outcome.duplicate_count += 1
                        previous = candidates[identity]
                        if content_version(data) <= content_version(previous):
                            continue
                    candidates[identity] = data
                return (
                    provider,
                    sorted(
                        candidates.values(),
                        key=lambda d: (
                            d.store_slug,
                            d.external_id,
                            d.country,
                            tuple(sorted(normalized_variant(d.variant).items())),
                        ),
                    ),
                    outcome,
                )
            except Exception:
                outcome.status = "NETWORK_FAILED"
                error(outcome, "provider_failed")
                structlog.get_logger().warning("search_provider_failed", provider=provider.name)
                return provider, [], outcome

        responses = await asyncio.gather(
            *(one(p) for _, p in sorted(self.registry.providers.items()))
        )
        # Interleave bounded provider slices; commercial preference never enters ranking.
        incoming = [
            entry
            for round_ in zip_longest(
                *[
                    [(provider, data, outcome) for data in offers]
                    for provider, offers, outcome in responses
                ]
            )
            for entry in round_
            if entry is not None
        ]
        persisted: set[UUID] = set()
        for i, (provider, data, outcome) in enumerate(incoming):
            if i >= settings.search_persistence_limit:
                error(outcome, "persistence_limit")
                continue
            if data.country != country or (currency and data.currency != currency):
                outcome.rejected_count += 1
                error(outcome, "market_mismatch")
                continue
            try:
                offer = await self.products.persist(
                    data, mode=search_ingestion(provider.capabilities)
                )
                persisted.add(offer.product_id)
                outcome.accepted_count += 1
            except Exception:
                outcome.rejected_count += 1
                error(outcome, "item_rejected")
                structlog.get_logger().warning(
                    "search_item_rejected", provider=provider.name, code="item_rejected"
                )
        comparisons = [
            await self.products.product(product_id, user_id, market_country=country)
            for product_id in sorted(persisted)[: settings.search_comparison_limit]
        ]
        comparisons.sort(key=lambda product: comparison_rank(product, query))
        outcomes = []
        for _, _, outcome in responses:
            if outcome.status != "NETWORK_FAILED":
                outcome.status = (
                    "PARTIAL"
                    if outcome.errors
                    else "SUCCESS"
                    if outcome.accepted_count
                    else "EMPTY"
                )
            try:
                await self.health.record(outcome)
            except Exception:
                structlog.get_logger().warning(
                    "provider_health_record_failed", provider=outcome.provider
                )
            outcomes.append(outcome)
        return SearchResult(
            products=comparisons[: limits.search_result_limit],
            unavailable_providers=sorted(
                o.provider for o in outcomes if o.status == "NETWORK_FAILED"
            ),
            provider_outcomes=outcomes,
        )
