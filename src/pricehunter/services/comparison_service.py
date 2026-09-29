from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import Product, ProductDiscovery, Store, StoreOffer, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.comparison import (
    ComparisonOffer,
    ComparisonProduct,
    CurrencyComparison,
    DiscoveryStatus,
)
from pricehunter.domain.errors import PriceHunterError, ProductNotFoundError
from pricehunter.domain.freshness import Freshness, OfferFreshnessPolicy
from pricehunter.domain.subscriptions import Feature
from pricehunter.services.entitlement_service import EntitlementService
from pricehunter.services.fx_service import FxService
from pricehunter.services.market_context import user_market
from pricehunter.services.outbound_service import OutboundLinkService


@dataclass
class ComparisonSummary:
    groups: list[CurrencyComparison]
    offer_count: int
    store_count: int
    confidence: Decimal
    next_expiry: datetime | None


class ComparisonReader:
    """Three summary queries regardless of listing count; offer bodies are paginated."""

    def __init__(
        self, settings: Settings, permission: str | None = None, market_country: str | None = None
    ) -> None:
        self.permission = permission
        self.market_country = market_country
        self.settings = settings
        self.outbound = OutboundLinkService(settings)
        base = OfferFreshnessPolicy(settings.offer_freshness_seconds)
        self.policy = OfferFreshnessPolicy(
            {
                **settings.offer_freshness_seconds,
                **{
                    name: min(base.ttl(name), policy.max_cache_seconds)
                    for name, policy in settings.provider_data_policies.items()
                },
            }
        )

    def ttl_expression(self) -> ColumnElement[int]:
        configured = self.policy.seconds
        if set(configured) == {"default"}:
            return literal(configured["default"])
        return case(
            *[
                (Store.provider_type == key, value)
                for key, value in configured.items()
                if key != "default"
            ],
            *[
                (func.split_part(Store.provider_type, "_", 1) == key, configured[key])
                for key in sorted(configured, key=len, reverse=True)
                if key != "default"
            ],
            else_=configured["default"],
        )

    def freshness_expression(self, now: datetime) -> ColumnElement[str]:
        ttl = self.ttl_expression()
        return case(
            (
                or_(
                    StoreOffer.failure_count > 0,
                    StoreOffer.suspicious_price.is_not(None),
                    StoreOffer.last_checked_at > now + timedelta(seconds=60),
                ),
                str(Freshness.FAILED),
            ),
            (
                and_(
                    ttl > 0,
                    StoreOffer.last_checked_at > now - func.make_interval(0, 0, 0, 0, 0, 0, ttl),
                ),
                str(Freshness.FRESH),
            ),
            else_=str(Freshness.STALE),
        )

    def filters(self, product_id: UUID, currency: str | None = None) -> list[ColumnElement[bool]]:
        filters = [
            StoreOffer.product_id == product_id,
            Store.active.is_(True),
            Store.supported.is_(True),
            or_(
                *[
                    and_(
                        Store.provider_type == name,
                        StoreOffer.last_checked_at
                        > utcnow() - timedelta(seconds=policy.max_cache_seconds),
                    )
                    for name, policy in {
                        "mock": self.settings.data_policy("mock"),
                        **self.settings.provider_data_policies,
                    }.items()
                    if policy.reviewed
                    and policy.catalog_persistence_allowed
                    and (self.permission is None or getattr(policy, self.permission))
                ]
            ),
        ]
        if self.market_country is not None:
            filters.append(StoreOffer.market_country == self.market_country)
        if currency:
            filters.append(StoreOffer.currency == currency)
        return filters

    def view(self, offer: StoreOffer, store: Store, now: datetime) -> ComparisonOffer:
        freshness = self.policy.classify(
            store.provider_type,
            offer.last_checked_at,
            now,
            failures=offer.failure_count,
            quarantined=offer.suspicious_price is not None,
        )
        policy = self.settings.data_policy(store.provider_type)
        try:
            url = self.outbound.link(offer, store, market_country=offer.market_country)
        except PriceHunterError:
            url = None
        return ComparisonOffer(
            offer_id=offer.id,
            store=store.name,
            store_slug=store.slug,
            store_country=offer.market_country,
            title=offer.title,
            price=offer.price,
            currency=offer.currency,
            availability=offer.availability,
            url=url,
            provider=store.provider_type,
            attribution=policy.display_attribution_required,
            image_url=offer.image_url,
            last_checked_at=offer.last_checked_at,
            freshness=freshness,
            age_seconds=max(0, int((now - offer.last_checked_at).total_seconds())),
            stale=freshness != Freshness.FRESH,
        )

    async def summary(
        self, session: AsyncSession, product_id: UUID, now: datetime, currency: str | None = None
    ) -> ComparisonSummary:
        freshness = self.freshness_expression(now)
        valid = and_(freshness == Freshness.FRESH, StoreOffer.availability == "in_stock")
        filters = self.filters(product_id, currency)
        stats = (
            await session.execute(
                select(
                    StoreOffer.currency,
                    func.count().filter(freshness == Freshness.FRESH).label("fresh"),
                    func.count().filter(freshness == Freshness.STALE).label("stale"),
                    func.count().filter(freshness == Freshness.FAILED).label("failed"),
                    func.count()
                    .filter(
                        and_(
                            freshness == Freshness.FRESH, StoreOffer.availability == "out_of_stock"
                        )
                    )
                    .label("fresh_out_of_stock"),
                    (
                        func.max(StoreOffer.price).filter(valid)
                        - func.min(StoreOffer.price).filter(valid)
                    ).label("spread"),
                )
                .join(Store)
                .where(*filters)
                .group_by(StoreOffer.currency)
                .order_by(StoreOffer.currency)
            )
        ).all()
        totals = (
            await session.execute(
                select(
                    func.count(),
                    func.count(func.distinct(StoreOffer.store_id)),
                    func.min(StoreOffer.match_confidence),
                    func.min(
                        StoreOffer.last_checked_at
                        + func.make_interval(0, 0, 0, 0, 0, 0, self.ttl_expression())
                    ).filter(freshness == Freshness.FRESH),
                )
                .select_from(StoreOffer)
                .join(Store)
                .where(*filters)
            )
        ).one()
        order = (StoreOffer.price, Store.slug, StoreOffer.id)
        ranked = (
            select(
                StoreOffer.id,
                freshness.label("freshness"),
                StoreOffer.availability,
                func.row_number()
                .over(partition_by=StoreOffer.currency, order_by=order)
                .label("known_rank"),
                func.row_number()
                .over(
                    partition_by=StoreOffer.currency, order_by=(case((valid, 0), else_=1), *order)
                )
                .label("best_rank"),
                func.row_number()
                .over(
                    partition_by=StoreOffer.currency,
                    order_by=(case((freshness == Freshness.STALE, 0), else_=1), *order),
                )
                .label("stale_rank"),
            )
            .join(Store)
            .where(*filters)
            .subquery()
        )
        rows = (
            await session.execute(
                select(
                    StoreOffer,
                    Store,
                    ranked.c.known_rank,
                    ranked.c.best_rank,
                    ranked.c.stale_rank,
                )
                .join(Store)
                .join(ranked, ranked.c.id == StoreOffer.id)
                .where(
                    or_(
                        ranked.c.known_rank == 1,
                        and_(
                            ranked.c.best_rank == 1,
                            ranked.c.freshness == Freshness.FRESH,
                            ranked.c.availability == "in_stock",
                        ),
                        and_(ranked.c.stale_rank == 1, ranked.c.freshness == Freshness.STALE),
                    )
                )
            )
        ).all()
        known, best, stale = {}, {}, {}
        for offer, store, known_rank, best_rank, stale_rank in rows:
            view = self.view(offer, store, now)
            if known_rank == 1:
                known[offer.currency] = view
            if (
                best_rank == 1
                and view.freshness == Freshness.FRESH
                and view.availability == "in_stock"
            ):
                best[offer.currency] = view
            if stale_rank == 1 and view.freshness == Freshness.STALE:
                stale[offer.currency] = view
        groups = [
            CurrencyComparison(
                currency=row.currency,
                best_available_offer=best.get(row.currency),
                cheapest_known_offer=known[row.currency],
                cheapest_stale_offer=stale.get(row.currency),
                price_spread=row.spread,
                fresh_offer_count=row.fresh,
                stale_offer_count=row.stale,
                failed_offer_count=row.failed,
                fresh_out_of_stock_count=row.fresh_out_of_stock,
            )
            for row in stats
            if row.currency in known
        ]
        return ComparisonSummary(groups, totals[0], totals[1], totals[2] or Decimal(0), totals[3])

    async def offers(
        self, session: AsyncSession, product_id: UUID, now: datetime, *, page: int, size: int
    ) -> list[ComparisonOffer]:
        freshness = self.freshness_expression(now)
        rows = (
            await session.execute(
                select(StoreOffer, Store)
                .join(Store)
                .where(*self.filters(product_id))
                .order_by(
                    StoreOffer.currency,
                    case((freshness == Freshness.FRESH, 0), else_=1),
                    case(
                        (StoreOffer.availability == "in_stock", 0),
                        (StoreOffer.availability == "unknown", 1),
                        else_=2,
                    ),
                    StoreOffer.price,
                    Store.slug,
                    StoreOffer.id,
                )
                .offset(max(0, page) * size)
                .limit(size)
            )
        ).all()
        return [self.view(offer, store, now) for offer, store in rows]


class ComparisonService:
    def __init__(
        self, sessions: SessionFactory, entitlements: EntitlementService, settings: Settings
    ) -> None:
        self.sessions, self.entitlements = sessions, entitlements
        self.reader = ComparisonReader(settings)
        self.fx = FxService(sessions, settings)

    async def get(
        self,
        product_id: UUID,
        user_id: UUID,
        *,
        page: int = 0,
        size: int = 10,
        market_country: str | None = None,
        permission: str | None = None,
    ) -> ComparisonProduct:
        (await self.entitlements.for_user(user_id)).entitlements.require(Feature.COMPARISON_SEARCH)
        async with self.sessions() as session:
            market = await user_market(session, user_id, market_country)
            result = await self.build(
                session,
                product_id,
                page=page,
                size=size,
                market_country=market,
                permission=permission,
            )
            user = await session.get(User, user_id)
            if user:
                result.preferred_currency = user.preferred_currency
                snapshot = await self.fx.snapshot(session)
                if snapshot:
                    views = [
                        *result.offers,
                        *[
                            o
                            for g in result.currency_groups
                            for o in (
                                g.best_available_offer,
                                g.cheapest_known_offer,
                                g.cheapest_stale_offer,
                            )
                            if o
                        ],
                    ]
                    for offer in views:
                        if offer.currency == user.preferred_currency:
                            continue
                        converted = snapshot.convert(
                            offer.price, offer.currency, user.preferred_currency
                        )
                        if converted is not None:
                            offer.reference_price, offer.reference_currency = (
                                converted,
                                user.preferred_currency,
                            )
                            offer.fx_rate = snapshot.cross_rate(
                                offer.currency, user.preferred_currency
                            )
                            offer.fx_effective_date, offer.fx_fetched_at, offer.fx_source = (
                                snapshot.effective_date,
                                snapshot.fetched_at,
                                snapshot.source,
                            )
            return result

    async def build(
        self,
        session: AsyncSession,
        product_id: UUID,
        *,
        page: int = 0,
        size: int = 10,
        market_country: str | None = None,
        permission: str | None = None,
    ) -> ComparisonProduct:
        product = await session.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError()
        now = utcnow()
        reader = ComparisonReader(
            self.reader.settings, permission=permission, market_country=market_country
        )
        summary = await reader.summary(session, product_id, now)
        groups = summary.groups
        size, page = min(max(size, 1), 50), max(page, 0)
        offers = await reader.offers(session, product_id, now, page=page, size=size)
        discoveries = list(
            await session.scalars(
                select(ProductDiscovery)
                .where(
                    ProductDiscovery.product_id == product_id,
                    *([ProductDiscovery.country == market_country] if market_country else []),
                )
                .order_by(
                    ProductDiscovery.provider, ProductDiscovery.country, ProductDiscovery.currency
                )
                .limit(100)
            )
        )
        return ComparisonProduct(
            id=product.id,
            market_country=market_country,
            canonical_name=product.canonical_name,
            brand=product.brand,
            gtin=product.gtin,
            model=product.model or product.mpn,
            variant=product.variant,
            image_url=next((o.image_url for o in offers if o.image_url), None),
            offers=offers,
            currencies=[g.currency for g in groups],
            currency_groups=groups,
            best_available_offer=groups[0].best_available_offer if len(groups) == 1 else None,
            price_spread=groups[0].price_spread if len(groups) == 1 else None,
            match_confidence=summary.confidence,
            store_count=summary.store_count,
            offer_count=summary.offer_count,
            page=page,
            page_size=size,
            discovery=[
                DiscoveryStatus(
                    provider=d.provider,
                    country=d.country,
                    currency=d.currency,
                    status="running"
                    if d.lease_until and d.lease_until > now
                    else "unsupported"
                    if d.last_error_code == "insufficient_identity"
                    else "backed_off"
                    if d.last_error_code
                    else "pending"
                    if d.last_success_at is None
                    else "empty"
                    if d.result_count == 0
                    else "ok",
                    last_success_at=d.last_success_at,
                    next_discovery_at=d.next_discovery_at,
                    error_code=d.last_error_code,
                )
                for d in discoveries
            ],
        )
