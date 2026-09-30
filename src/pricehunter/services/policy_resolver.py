from datetime import datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    FeedSyncState,
    MerchantFeedItem,
    MerchantProgram,
    Store,
    StoreOffer,
)
from pricehunter.domain.errors import ProviderPolicyError
from pricehunter.domain.feeds import FEED_NETWORKS, FeedProductData, merchant_slug
from pricehunter.domain.ingestion import FeedRevalidationContext
from pricehunter.domain.products import ProductOfferData
from pricehunter.domain.provider_policy import ProviderDataPolicy


class PolicyResolver:
    """One merchant/direct-provider boundary, shared by SQL and object-level decisions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def network_allowed(self, network: str, permission: str) -> bool:
        if network in FEED_NETWORKS:
            return bool(getattr(self.settings, f"{network}_enabled", False))
        return bool(getattr(self.settings.data_policy(network), permission, False))

    def program(self, program: MerchantProgram | None) -> ProviderDataPolicy:
        if program is None or not program.active or not program.approved:
            return ProviderDataPolicy()
        if not self.network_allowed(program.network, "catalog_persistence_allowed"):
            return ProviderDataPolicy()
        if self.settings.feed_program_ids and str(program.id) not in self.settings.feed_program_ids:
            return ProviderDataPolicy()
        try:
            return ProviderDataPolicy.model_validate(program.policy_data)
        except ValidationError:
            return ProviderDataPolicy()

    def offer(self, offer: StoreOffer, store: Store) -> ProviderDataPolicy:
        if store.provider_type in FEED_NETWORKS or offer.merchant_program_id is not None:
            program = offer.merchant_program
            if program is None or (program.network, program.store_id, program.market_country) != (
                store.provider_type,
                offer.store_id,
                offer.market_country,
            ):
                return ProviderDataPolicy()
            return self.program(program)
        return self.settings.data_policy(store.provider_type)

    async def incoming(self, session: AsyncSession, data: ProductOfferData) -> ProviderDataPolicy:
        if data.provider not in FEED_NETWORKS and data.merchant_program_id is None:
            return self.settings.data_policy(data.provider)
        program = (
            await session.get(MerchantProgram, data.merchant_program_id, with_for_update=True)
            if data.merchant_program_id
            else None
        )
        if program is None or (
            program.network,
            program.external_merchant_id,
            program.market_country,
            program.currency,
            merchant_slug(program.network, program.external_merchant_id),
        ) != (
            data.provider,
            data.external_merchant_id,
            data.country,
            data.currency,
            data.store_slug,
        ):
            raise ProviderPolicyError()
        policy = self.program(program)
        policy.require("catalog_persistence_allowed")
        current = await session.scalar(
            select(MerchantFeedItem.id).where(
                MerchantFeedItem.merchant_program_id == program.id,
                MerchantFeedItem.external_id == data.external_id,
                MerchantFeedItem.feed_generation == data.feed_generation,
                MerchantFeedItem.active.is_(True),
                MerchantFeedItem.seen_at > utcnow() - timedelta(seconds=policy.max_cache_seconds),
            )
        )
        if current is None:
            raise ProviderPolicyError()
        return policy

    async def snapshot_context(
        self, session: AsyncSession, data: ProductOfferData
    ) -> FeedRevalidationContext | None:
        if data.merchant_program_id is None:
            return None
        # Locks the program against policy edits and generation publication until commit.
        await self.incoming(session, data)
        program = await session.get(MerchantProgram, data.merchant_program_id)
        assert program is not None
        item = await session.scalar(
            select(MerchantFeedItem)
            .join(
                FeedSyncState,
                FeedSyncState.merchant_program_id == MerchantFeedItem.merchant_program_id,
            )
            .where(
                MerchantFeedItem.merchant_program_id == program.id,
                MerchantFeedItem.external_id == data.external_id,
                MerchantFeedItem.active.is_(True),
                MerchantFeedItem.feed_generation == data.feed_generation,
                MerchantFeedItem.feed_generation == FeedSyncState.generation,
                FeedSyncState.generation > 0,
                MerchantFeedItem.seen_at <= FeedSyncState.last_success_at,
                MerchantFeedItem.seen_at <= utcnow(),
            )
        )
        if item is None:
            raise ProviderPolicyError()
        authoritative = FeedProductData.model_validate(item.data).offer(
            program_id=program.id,
            network=program.network,
            merchant_id=program.external_merchant_id,
            name=program.display_name,
            domain=program.domain,
            market=program.market_country,
            generation=item.feed_generation,
        )
        if data != authoritative:
            # A copied generation ID cannot turn altered/stale search data into evidence.
            raise ProviderPolicyError()
        return FeedRevalidationContext(
            program.id, item.external_id, item.feed_generation, item.seen_at
        )

    def program_filter(self, permission: str) -> ColumnElement[bool]:
        p = MerchantProgram
        enabled = [n for n in FEED_NETWORKS if self.network_allowed(n, permission)]
        clauses = [
            p.network.in_(enabled),
            p.active.is_(True),
            p.approved.is_(True),
            p.policy_data["reviewed"].as_boolean().is_(True),
            p.policy_data["catalog_persistence_allowed"].as_boolean().is_(True),
            p.policy_data[permission].as_boolean().is_(True),
            func.length(p.policy_data["review_reference"].as_string()) > 0,
        ]
        if self.settings.feed_program_ids:
            clauses.append(p.id.in_([UUID(v) for v in self.settings.feed_program_ids]))
        return and_(*clauses)

    def allowed(
        self, permission: str, *, now: datetime | None = None, current_feed: bool = True
    ) -> ColumnElement[bool]:
        direct = []
        for name, policy in {
            "mock": self.settings.data_policy("mock"),
            **self.settings.provider_data_policies,
        }.items():
            if name not in FEED_NETWORKS and policy.reviewed and getattr(policy, permission, False):
                clause = and_(Store.provider_type == name, StoreOffer.merchant_program_id.is_(None))
                if now:
                    clause = and_(
                        clause,
                        StoreOffer.last_checked_at
                        > now - timedelta(seconds=policy.max_cache_seconds),
                    )
                direct.append(clause)
        conditions = [
            MerchantProgram.id == StoreOffer.merchant_program_id,
            MerchantProgram.store_id == StoreOffer.store_id,
            MerchantProgram.network == Store.provider_type,
            MerchantProgram.market_country == StoreOffer.market_country,
            self.program_filter(permission),
        ]
        if now and current_feed:
            conditions.append(
                exists()
                .where(
                    MerchantFeedItem.merchant_program_id == MerchantProgram.id,
                    MerchantFeedItem.external_id == StoreOffer.external_id,
                    MerchantFeedItem.active.is_(True),
                )
                .correlate(MerchantProgram, StoreOffer)
            )
        if now:
            conditions.append(
                StoreOffer.last_checked_at
                > now
                - func.make_interval(
                    0, 0, 0, 0, 0, 0, MerchantProgram.policy_data["max_cache_seconds"].as_integer()
                )
            )
        return or_(*direct, exists().where(*conditions))

    def ttl(self, fallback: ColumnElement[int]) -> ColumnElement[int]:
        merchant_ttl = (
            select(MerchantProgram.policy_data["max_cache_seconds"].as_integer())
            .where(MerchantProgram.id == StoreOffer.merchant_program_id)
            .scalar_subquery()
        )
        return case(
            (
                StoreOffer.merchant_program_id.is_not(None),
                func.least(fallback, func.coalesce(merchant_ttl, literal(0))),
            ),
            else_=fallback,
        )
