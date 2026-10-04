from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    Product,
    ProductBestState,
    ProductWatch,
    Store,
    StoreOffer,
)
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import ComparisonOffer
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.subscriptions import Feature
from pricehunter.services.comparison_service import ComparisonReader, ComparisonSummary
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.market_context import user_market
from pricehunter.services.policy_resolver import PolicyResolver


class BestHistoryPoint(BaseModel):
    timestamp: datetime
    price: Decimal | None
    offer_id: UUID | None
    merchant_id: UUID | None = None
    store: str | None
    event_type: str


class BestHistory(BaseModel):
    product_id: UUID
    canonical_name: str
    market_country: str
    currency: str
    current_best: ComparisonOffer | None
    minimum: Decimal | None
    retention_days: int
    points: list[BestHistoryPoint]


class BestPriceService:
    def __init__(
        self, sessions: SessionFactory, entitlements: EntitlementService, settings: Settings
    ) -> None:
        self.sessions, self.entitlements, self.settings = sessions, entitlements, settings
        self.reader = ComparisonReader(settings, permission="price_history_allowed")

    async def record(
        self,
        session: AsyncSession,
        product_id: UUID,
        summary: ComparisonSummary,
        now: datetime,
        source_observation_id: UUID | None = None,
        *,
        market_country: str,
    ) -> None:
        """Caller holds the Product lock. State and events share the observation transaction."""
        states = {
            s.currency: s
            for s in await session.scalars(
                select(ProductBestState).where(
                    ProductBestState.product_id == product_id,
                    ProductBestState.market_country == market_country,
                )
            )
        }
        groups = {g.currency: g for g in summary.groups}
        watched = set(
            await session.scalars(
                select(ProductWatch.currency).where(
                    ProductWatch.product_id == product_id,
                    ProductWatch.market_country == market_country,
                    ProductWatch.enabled.is_(True),
                )
            )
        )
        for currency in sorted(states.keys() | groups.keys() | watched):
            best = groups[currency].best_available_offer if currency in groups else None
            # The supplied summary is already filtered by current history permission.
            state = states.get(currency)
            is_new = state is None
            if state is None:
                state = ProductBestState(
                    product_id=product_id,
                    market_country=market_country,
                    currency=currency,
                    sequence=0,
                )
                session.add(state)
            state.next_evaluation_at = summary.next_expiry
            current = (best.offer_id, best.price) if best else (None, None)
            merchant_id = best.merchant_id if best else None
            previous_merchant, previous_price = state.merchant_id, state.price
            if not is_new and (state.store_offer_id, state.price, state.merchant_id) == (
                *current,
                merchant_id,
            ):
                continue
            if (
                not is_new
                and state.store_offer_id
                and best
                and previous_merchant == merchant_id
                and previous_price == best.price
            ):
                # Same retailer/price, new acquisition source: retain exact current
                # provenance without inventing a merchant/history transition.
                state.store_offer_id = best.offer_id
                state.observed_at = now
                continue
            event_type = (
                "initial_best"
                if is_new and best
                else "became_unavailable"
                if not best
                else "restored"
                if state.store_offer_id is None
                else "merchant_changed"
                if state.merchant_id != merchant_id
                else "price_changed"
            )
            state.store_offer_id, state.price = current
            state.merchant_id = merchant_id
            state.sequence += 1
            state.observed_at = now
            session.add(
                BestPriceEvent(
                    product_id=product_id,
                    market_country=market_country,
                    currency=currency,
                    sequence=state.sequence,
                    store_offer_id=state.store_offer_id,
                    merchant_id=state.merchant_id,
                    price=state.price,
                    store=best.store if best else None,
                    event_type=event_type,
                    observed_at=now,
                    source_observation_id=source_observation_id,
                )
            )
            structlog.get_logger().info(
                "best_merchant_changed"
                if event_type == "merchant_changed"
                else "best_price_changed",
                product_id=str(product_id),
                currency=currency,
                offer_id=str(state.store_offer_id) if best else None,
                transition=event_type,
            )

    async def history(
        self,
        product_id: UUID,
        user_id: UUID,
        currency: str,
        limit: int = 50,
        *,
        market_country: str | None = None,
    ) -> BestHistory:
        rights = (await self.entitlements.for_user(user_id)).entitlements
        rights.require(Feature.HISTORY)
        days = min(rights.history_days, self.settings.history_retention_days or rights.history_days)
        now = utcnow()
        cutoff = now - timedelta(days=days)
        async with self.sessions() as session:
            market = await user_market(session, user_id, market_country)
            reader = ComparisonReader(
                self.settings, permission="price_history_allowed", market_country=market
            )
            product = await session.get(Product, product_id)
            if product is None:
                raise ProductNotFoundError()
            conditions = [
                BestPriceEvent.product_id == product_id,
                BestPriceEvent.currency == currency,
                BestPriceEvent.market_country == market,
                or_(
                    BestPriceEvent.store_offer_id.is_(None),
                    BestPriceEvent.store_offer_id.in_(
                        select(StoreOffer.id)
                        .join(Store)
                        .where(PolicyResolver(self.settings).allowed("price_history_allowed"))
                    ),
                ),
            ]
            recent = list(
                await session.scalars(
                    select(BestPriceEvent)
                    .where(*conditions, BestPriceEvent.observed_at >= cutoff)
                    .order_by(BestPriceEvent.observed_at.desc(), BestPriceEvent.sequence.desc())
                    .limit(min(max(limit, 1), 200))
                )
            )
            low = await session.scalar(
                select(func.min(BestPriceEvent.price)).where(
                    *conditions, BestPriceEvent.observed_at >= cutoff
                )
            )
            # The last transition before the window describes its starting state without
            # exposing older timestamps or an unbounded lifetime history.
            anchor = await session.scalar(
                select(BestPriceEvent)
                .where(*conditions, BestPriceEvent.observed_at < cutoff)
                .order_by(BestPriceEvent.observed_at.desc(), BestPriceEvent.sequence.desc())
                .limit(1)
            )
            points = [
                BestHistoryPoint(
                    timestamp=e.observed_at,
                    price=e.price,
                    offer_id=e.store_offer_id,
                    merchant_id=e.merchant_id,
                    store=e.store,
                    event_type=e.event_type,
                )
                for e in reversed(recent)
            ]
            if anchor and len(points) < min(max(limit, 1), 200):
                points.insert(
                    0,
                    BestHistoryPoint(
                        timestamp=cutoff,
                        price=anchor.price,
                        offer_id=anchor.store_offer_id,
                        merchant_id=anchor.merchant_id,
                        store=anchor.store,
                        event_type="window_start",
                    ),
                )
            summary = await reader.summary(session, product_id, now, currency)
            best = summary.groups[0].best_available_offer if summary.groups else None
            values = [
                v
                for v in (low, anchor.price if anchor else None, best.price if best else None)
                if v is not None
            ]
            return BestHistory(
                product_id=product_id,
                canonical_name=product.canonical_name,
                market_country=market,
                currency=currency,
                current_best=best,
                minimum=min(values) if values else None,
                retention_days=days,
                points=points,
            )

    async def prune(self) -> int:
        days = self.settings.history_retention_days
        if days <= 0:
            return 0
        cutoff = utcnow() - timedelta(days=days)
        async with self.sessions.begin() as session:
            # Rank per series: retain one boundary anchor, including unavailable states.
            ranked = (
                select(
                    BestPriceEvent.id,
                    func.row_number()
                    .over(
                        partition_by=(
                            BestPriceEvent.product_id,
                            BestPriceEvent.market_country,
                            BestPriceEvent.currency,
                        ),
                        order_by=BestPriceEvent.sequence.desc(),
                    )
                    .label("position"),
                )
                .where(BestPriceEvent.observed_at < cutoff)
                .subquery()
            )
            ids = select(ranked.c.id).where(ranked.c.position > 1).limit(5000)
            result = await session.execute(
                delete(BestPriceEvent)
                .where(BestPriceEvent.id.in_(ids))
                .returning(BestPriceEvent.id)
            )
            return len(result.all())
