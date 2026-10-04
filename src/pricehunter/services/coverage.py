import asyncio
from contextlib import nullcontext
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import aliased

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    FeedSyncState,
    MerchantFeedItem,
    MerchantProgram,
    Store,
    StoreOffer,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.feeds import FEED_NETWORKS
from pricehunter.domain.markets import validate_country
from pricehunter.domain.products import ProductOfferData
from pricehunter.providers.registry import ProviderRegistry
from pricehunter.services.comparison_service import ComparisonReader
from pricehunter.services.policy_resolver import PolicyResolver
from pricehunter.services.provider_health import ProviderHealth


class CoverageDiagnostics:
    """Operator-only aggregate/read-only reports, with bounded candidate retrieval."""

    def __init__(
        self,
        sessions: SessionFactory,
        settings: Settings,
        registry: ProviderRegistry,
        limiter: RateLimiter,
    ) -> None:
        self.sessions, self.settings, self.registry, self.limiter = (
            sessions,
            settings,
            registry,
            limiter,
        )
        self.policy = PolicyResolver(settings)
        self.reader = ComparisonReader(settings)
        self.health = ProviderHealth(limiter.redis)

    def configured(self, name: str) -> bool:
        s = self.settings
        if name == "cj":
            return bool(
                s.cj_api_token.get_secret_value().strip() and s.cj_company_id and s.cj_website_id
            )
        if name == "awin":
            return bool(s.awin_feed_api_key.get_secret_value().strip())
        if name == "tradedoubler":
            return bool(s.tradedoubler_token.get_secret_value().strip())
        return name in self.registry.providers

    async def report(self) -> dict[str, Any]:
        now = utcnow()
        catalog = and_(
            Store.active,
            Store.supported,
            StoreOffer.catalog_active,
            self.policy.allowed("catalog_persistence_allowed", now=now),
        )
        async with self.sessions() as session:
            programs = (
                await session.execute(
                    select(
                        MerchantProgram.market_country,
                        MerchantProgram.network,
                        func.count().label("programs"),
                        func.count()
                        .filter(self.policy.program_filter("catalog_persistence_allowed"))
                        .label("programs_active"),
                    ).group_by(MerchantProgram.market_country, MerchantProgram.network)
                )
            ).all()
            staged = (
                await session.execute(
                    select(
                        MerchantProgram.market_country,
                        MerchantProgram.network,
                        func.count().filter(MerchantFeedItem.active).label("staged_active"),
                        func.count()
                        .filter(
                            and_(
                                MerchantFeedItem.active,
                                self.policy.program_filter("catalog_persistence_allowed"),
                                MerchantFeedItem.seen_at
                                > now
                                - func.make_interval(
                                    0,
                                    0,
                                    0,
                                    0,
                                    0,
                                    0,
                                    MerchantProgram.policy_data["max_cache_seconds"].as_integer(),
                                ),
                            )
                        )
                        .label("staged_usable"),
                    )
                    .select_from(MerchantFeedItem)
                    .join(MerchantProgram)
                    .group_by(MerchantProgram.market_country, MerchantProgram.network)
                )
            ).all()
            offers = (
                await session.execute(
                    select(
                        StoreOffer.market_country,
                        Store.provider_type,
                        func.count().label("materialized_offers"),
                        func.count(func.distinct(StoreOffer.product_id)).label(
                            "canonical_products"
                        ),
                        func.count().filter(catalog).label("catalog_eligible_offers"),
                        func.count()
                        .filter(and_(catalog, self.reader.freshness_expression(now) == "fresh"))
                        .label("fresh_offers"),
                        func.count()
                        .filter(and_(catalog, self.policy.allowed("tracking_allowed", now=now)))
                        .label("tracking_eligible_offers"),
                        func.count()
                        .filter(self.policy.allowed("price_history_allowed"))
                        .label("history_eligible_offers"),
                    )
                    .select_from(StoreOffer)
                    .join(Store)
                    .group_by(StoreOffer.market_country, Store.provider_type)
                )
            ).all()
            feed_health = (
                await session.execute(
                    select(
                        MerchantProgram.network,
                        func.max(FeedSyncState.last_success_at).label("last_successful_feed_sync"),
                        func.max(FeedSyncState.last_failure_at).label("last_feed_failure"),
                        func.max(FeedSyncState.failure_count).label(
                            "max_program_consecutive_failures"
                        ),
                    )
                    .select_from(FeedSyncState)
                    .join(MerchantProgram)
                    .group_by(MerchantProgram.network)
                )
            ).all()
        markets: dict[str, dict[str, Any]] = {}
        defaults = dict.fromkeys(
            (
                "programs",
                "programs_active",
                "staged_active",
                "staged_usable",
                "materialized_offers",
                "canonical_products",
                "catalog_eligible_offers",
                "fresh_offers",
                "tracking_eligible_offers",
                "history_eligible_offers",
            ),
            0,
        )
        for rows in (programs, staged, offers):
            for row in rows:
                country, network, *values = row
                entry = markets.setdefault(country, {}).setdefault(
                    network, {**defaults, "enabled": network in self.registry.providers}
                )
                entry.update(dict(zip(list(row._mapping)[2:], values, strict=True)))
        networks = (
            set(FEED_NETWORKS) | set(self.registry.providers) | {r.network for r in feed_health}
        )
        providers: dict[str, Any] = {}
        for network in sorted(networks):
            try:
                health = await self.health.read(network)
            except Exception:
                health = {"search_health_unavailable": True}
            providers[network] = {
                "enabled": network in self.registry.providers,
                "configured": self.configured(network),
                "search_health": health,
                **next((dict(r._mapping) for r in feed_health if r.network == network), {}),
            }
        return {"providers": providers, "markets": markets}

    async def product(self, query: str, country: str) -> dict[str, Any]:
        if not query.strip() or len(query) > 200 or len(country) != 2:
            raise ValueError("A bounded query and market are required")
        validate_country(country)
        output: list[dict[str, Any]] = []
        remaining = self.settings.search_persistence_limit
        for name, provider in sorted(self.registry.providers.items()):
            if remaining <= 0:
                break
            try:
                async with (
                    asyncio.timeout(self.settings.provider_timeout_seconds),
                    (
                        nullcontext()
                        if provider.manages_request_limits
                        else self.limiter.provider(name)
                    ),
                ):
                    results = await provider.search(query, country=country)
                candidates = [
                    d
                    for d in results[
                        : min(remaining, self.settings.search_provider_candidate_limit)
                    ]
                    if isinstance(d, ProductOfferData)
                    and d.country == country
                    and d.provider == name
                ]
            except Exception:
                output.append({"provider": name, "status": "NETWORK_FAILED"})
                continue
            remaining -= len(candidates)
            groups: dict[str, list[ProductOfferData]] = {}
            for d in candidates:
                groups.setdefault(d.store_slug, []).append(d)
            async with self.sessions() as session:
                identities = [(d.store_slug, d.external_id, country) for d in candidates]
                rows = (
                    (
                        await session.execute(
                            select(StoreOffer, Store)
                            .join(Store)
                            .where(
                                tuple_(
                                    Store.slug, StoreOffer.external_id, StoreOffer.market_country
                                ).in_(identities)
                            )
                            .limit(self.settings.search_provider_candidate_limit)
                        )
                    ).all()
                    if identities
                    else []
                )
                for slug, items in sorted(groups.items()):
                    representative = items[0]
                    program = (
                        await session.get(MerchantProgram, representative.merchant_program_id)
                        if representative.merchant_program_id
                        else None
                    )
                    # Candidate visibility never grants persistent use or canonical matching.
                    policy = (
                        self.policy.program(program) if program else self.settings.data_policy(name)
                    )
                    materialized = [(o, s) for o, s in rows if s.slug == slug]
                    output.append(
                        {
                            "provider": name,
                            "merchant": representative.store_name,
                            "candidate_count": len(items),
                            "materialized_offers": len(materialized),
                            "fresh_offers": sum(
                                self.reader.view(o, s, utcnow()).freshness == "fresh"
                                for o, s in materialized
                            ),
                            "tracking_eligible": policy.tracking_allowed,
                            "history_eligible": policy.price_history_allowed,
                        }
                    )
        return {"market": country, "results": output}

    async def duplicate_merchants(self, limit: int = 20) -> list[dict[str, Any]]:
        a, b = aliased(Store), aliased(Store)

        def domain(value: Any) -> Any:
            return func.regexp_replace(func.lower(value), r"^www\.|\.$", "", "g")

        def name(value: Any) -> Any:
            return func.regexp_replace(func.lower(value), "[^[:alnum:]]", "", "g")

        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(a, b)
                    .join(
                        b,
                        and_(
                            a.id < b.id,
                            a.provider_type != b.provider_type,
                            or_(domain(a.domain) == domain(b.domain), name(a.name) == name(b.name)),
                        ),
                    )
                    .order_by(a.id, b.id)
                    .limit(min(max(limit, 1), 100))
                )
            ).all()
            return [
                {
                    "left": {"id": str(x.id), "network": x.provider_type, "merchant": x.name},
                    "right": {"id": str(y.id), "network": y.provider_type, "merchant": y.name},
                    "reason": "possible_duplicate_only",
                }
                for x, y in rows
            ]
