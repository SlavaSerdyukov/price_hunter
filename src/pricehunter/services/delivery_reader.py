"""SQL delivery representatives; constant query count, paginated source bodies."""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, case, cast, column, func, or_, select, union_all, values
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from pricehunter.db.models import DeliveryQuote, Store, StoreOffer
from pricehunter.domain.comparison import ComparisonOffer, ComparisonProduct
from pricehunter.domain.delivery import DeliveryContext, DeliveryStatus, TaxStatus
from pricehunter.domain.freshness import Freshness
from pricehunter.services.policy_resolver import PolicyResolver

if TYPE_CHECKING:
    from pricehunter.services.comparison_service import ComparisonReader


class DeliveryReader:
    def __init__(
        self,
        reader: "ComparisonReader",
        context: DeliveryContext,
        now: datetime,
        request_quotes: list[dict[str, object]] | None = None,
    ) -> None:
        self.reader, self.context, self.now = reader, context, now
        self.request_quotes = request_quotes or []

    def candidates(self, product_id: UUID) -> CTE:
        o, context, now = StoreOffer, self.context, self.now
        interval = self.quote_interval()
        columns = list(DeliveryQuote.__table__.columns)
        cache_select = (
            select(*columns)
            .join(StoreOffer, StoreOffer.id == DeliveryQuote.offer_id)
            .where(StoreOffer.product_id == product_id)
        )
        if self.request_quotes:
            ephemeral = values(
                *[column(c.name, c.type) for c in columns], name="request_delivery_quotes"
            ).data([tuple(row[c.name] for c in columns) for row in self.request_quotes])
            cache_source = union_all(
                cache_select,
                select(*[cast(ephemeral.c[c.name], c.type).label(c.name) for c in columns]),
            ).subquery("delivery_source_cache")
        else:
            cache_source = cache_select.subquery("delivery_source_cache")
        q = cache_source.c
        cache_current = and_(q.quoted_at <= now, q.expires_at > now, q.quoted_at + interval > now)
        cache_ranks = (
            select(
                cache_source,
                func.row_number()
                .over(
                    partition_by=q.offer_id,
                    order_by=(
                        case(
                            (and_(cache_current, q.status.not_in(["failed", "unsupported"])), 0),
                            (cache_current, 1),
                            else_=2,
                        ),
                        case((q.scope == "exact", 0), else_=1),
                        q.quoted_at.desc(),
                        q.id,
                    ),
                )
                .label("quote_rank"),
            )
            .where(
                q.offer_id == StoreOffer.id,
                Store.id == StoreOffer.store_id,
                q.item_price == StoreOffer.price,
                q.currency == StoreOffer.currency,
                q.offer_url == StoreOffer.url,
                q.country == context.country,
                or_(
                    and_(q.scope == "exact", q.destination_key == context.fingerprint),
                    and_(q.scope == "country", q.destination_key == context.country_key),
                ),
            )
            .cte("delivery_cache_ranks")
        )
        cached = select(cache_ranks).where(cache_ranks.c.quote_rank == 1).subquery()
        static_match = and_(
            o.delivery_country == context.country,
            o.delivery_currency == o.currency,
            o.delivery_item_price == o.price,
            or_(
                and_(
                    o.delivery_scope == "country", o.delivery_destination_key == context.country_key
                ),
                and_(
                    o.delivery_scope == "exact", o.delivery_destination_key == context.fingerprint
                ),
            ),
        )
        static_current = and_(
            static_match,
            o.delivery_quoted_at <= now,
            o.delivery_expires_at > now,
            o.delivery_quoted_at + interval > now,
        )
        dynamic_bound = and_(
            cached.c.item_price == o.price,
            cached.c.currency == o.currency,
            cached.c.offer_url == o.url,
        )
        dynamic_current = and_(
            dynamic_bound,
            cached.c.quoted_at <= now,
            cached.c.expires_at > now,
            cached.c.quoted_at + interval > now,
        )
        # A current non-terminal tuple is authoritative even when it reports
        # incomplete costs or destination unavailability. A failed lookup is not.
        dynamic_priority = case(
            (and_(dynamic_current, cached.c.status.not_in(["failed", "unsupported"])), 0),
            (dynamic_current, 1),
            else_=2,
        )
        static_priority = case(
            (static_current, 0),
            (and_(static_match, o.delivery_quoted_at.is_not(None)), 2),
            else_=3,
        )
        dynamic_scope = case((cached.c.scope == "exact", 0), else_=1)
        static_scope = case((o.delivery_scope == "exact", 0), else_=1)
        # Compare freshness, then scope, then time. Static wins an equal-time tie.
        # Select an entire evidence tuple, never coalesce individual source costs.
        use_dynamic = func.coalesce(
            and_(
                dynamic_bound,
                or_(
                    dynamic_priority < static_priority,
                    and_(
                        dynamic_priority == static_priority,
                        or_(
                            dynamic_scope < static_scope,
                            and_(
                                dynamic_scope == static_scope,
                                cached.c.quoted_at > o.delivery_quoted_at,
                            ),
                        ),
                    ),
                ),
            ),
            False,
        )

        def chosen(dynamic: object, static: object) -> ColumnElement[Any]:
            return case((use_dynamic, dynamic), else_=static)

        shipping = chosen(cached.c.shipping_price, o.shipping_price)
        tax_status = chosen(cached.c.tax_status, o.tax_status)
        tax = chosen(cached.c.tax_amount, o.tax_amount)
        quote_at = chosen(cached.c.quoted_at, o.delivery_quoted_at)
        expiry = chosen(cached.c.expires_at, o.delivery_expires_at)
        availability = chosen(cached.c.availability, o.delivery_availability)
        current = func.coalesce(chosen(dynamic_current, static_current), False)
        native_total = chosen(cached.c.delivered_total, o.delivery_total)
        terminal = and_(use_dynamic, cached.c.status.in_(["failed", "unsupported"]))
        complete = and_(
            current,
            ~func.coalesce(terminal, False),
            availability == "in_stock",
            shipping.is_not(None),
            shipping >= 0,
            tax_status.in_(["included", "not_applicable", "additional"]),
            or_(tax_status != "additional", and_(tax.is_not(None), tax >= 0)),
            native_total.is_not(None),
        )
        total = case((complete, native_total), else_=None)
        fresh = self.reader.freshness_expression(now)
        eligible = and_(complete, fresh == Freshness.FRESH, o.availability == "in_stock")
        has_static = or_(
            o.shipping_price.is_not(None), o.delivery_scope != "unknown", o.tax_status != "unknown"
        )
        status = case(
            (terminal, cached.c.status),
            (
                and_(
                    quote_at.is_not(None),
                    ~current,
                    func.coalesce(chosen(dynamic_bound, static_match), False),
                ),
                "stale",
            ),
            (complete, "complete"),
            (or_(use_dynamic, has_static), "incomplete"),
            else_="unsupported",
        )
        return (
            select(
                o.id.label("offer_id"),
                o.currency,
                o.price,
                Store.merchant_id,
                total.label("total"),
                eligible.label("eligible"),
                status.label("status"),
                shipping.label("shipping"),
                tax_status.label("tax_status"),
                tax.label("tax"),
                quote_at.label("quote_at"),
                expiry.label("expiry"),
                availability.label("delivery_availability"),
                func.row_number()
                .over(
                    partition_by=(Store.merchant_id, o.currency),
                    order_by=(
                        case((eligible, 0), else_=1),
                        total.asc().nulls_last(),
                        case((fresh == Freshness.FRESH, 0), else_=1),
                        case((o.availability == "in_stock", 0), else_=1),
                        o.price,
                        o.match_confidence.desc(),
                        o.last_checked_at.desc(),
                        Store.id,
                        o.id,
                    ),
                )
                .label("merchant_rank"),
            )
            .join(Store)
            .outerjoin(cached, cached.c.offer_id == o.id)
            .where(*self.reader.filters(product_id))
            .cte("delivery_candidates")
            .prefix_with("MATERIALIZED")
        )

    def quote_interval(self) -> ColumnElement[Any]:
        ttl = PolicyResolver(self.reader.settings).ttl(
            case(
                *[
                    (
                        Store.provider_type == name,
                        min(
                            policy.max_cache_seconds,
                            self.reader.settings.delivery_quote_ttl_seconds,
                        ),
                    )
                    for name, policy in self.reader.settings.provider_data_policies.items()
                ],
                else_=self.reader.settings.delivery_quote_ttl_seconds,
            )
        )
        return func.make_interval(0, 0, 0, 0, 0, 0, ttl)

    @staticmethod
    def annotate(view: ComparisonOffer, row: object) -> ComparisonOffer:
        # SQLAlchemy RowMapping is read-only; values belong to this exact source.
        from sqlalchemy.engine import RowMapping

        assert isinstance(row, RowMapping)
        return view.model_copy(
            update={
                "delivered_total": row["total"],
                "shipping_price": row["shipping"],
                "delivery_country": None,
                "tax_status": TaxStatus(row["tax_status"]),
                "additional_tax": row["tax"] if row["tax_status"] == "additional" else None,
                "tax_included": True if row["tax_status"] == "included" else None,
                "delivery_quote_status": DeliveryStatus(row["status"]),
                "delivery_quote_at": row["quote_at"],
                "delivery_quote_expires_at": row["expiry"],
                "delivery_availability": row["delivery_availability"],
            }
        )

    async def enrich(self, session: AsyncSession, result: ComparisonProduct) -> None:
        result.delivery_country = self.context.country
        candidates = self.candidates(result.id)
        representatives = (
            select(candidates).where(candidates.c.merchant_rank == 1).cte("delivery_merchants")
        )
        c = representatives.c
        currency_ranks = select(
            representatives,
            func.row_number()
            .over(
                partition_by=c.currency,
                order_by=(
                    case((c.eligible, 0), else_=1),
                    c.total.asc().nulls_last(),
                    c.price,
                    c.merchant_id,
                    c.offer_id,
                ),
            )
            .label("currency_rank"),
            func.count().filter(c.eligible).over(partition_by=c.currency).label("delivered_count"),
            (
                func.max(c.total).filter(c.eligible).over(partition_by=c.currency)
                - func.min(c.total).filter(c.eligible).over(partition_by=c.currency)
            ).label("delivered_spread"),
        ).cte("delivery_currencies")
        rows = (
            await session.execute(
                select(StoreOffer, Store, currency_ranks)
                .join(Store)
                .join(currency_ranks, currency_ranks.c.offer_id == StoreOffer.id)
                .where(currency_ranks.c.currency_rank == 1)
            )
        ).all()
        for record in rows:
            offer, store = record[0], record[1]
            row = record._mapping
            group = next((g for g in result.currency_groups if g.currency == offer.currency), None)
            if group is not None:
                group.delivered_offer_count = row["delivered_count"]
                group.delivered_price_spread = row["delivered_spread"]
                if row["eligible"]:
                    group.best_delivered_offer = self.annotate(
                        self.reader.view(offer, store, self.now), row
                    )
        rows = (
            await session.execute(
                select(StoreOffer, Store, representatives)
                .join(Store)
                .join(representatives, c.offer_id == StoreOffer.id)
                .order_by(
                    c.currency,
                    case((c.eligible, 0), else_=1),
                    c.total.asc().nulls_last(),
                    c.price,
                    c.merchant_id,
                    c.offer_id,
                )
                .offset(result.page * result.page_size)
                .limit(result.page_size)
            )
        ).all()
        result.delivered_offers = [
            self.annotate(self.reader.view(r[0], r[1], self.now), r._mapping) for r in rows
        ]
        item_views = [
            *result.offers,
            *[
                o
                for g in result.currency_groups
                for o in (g.best_available_offer, g.cheapest_known_offer, g.cheapest_stale_offer)
                if o
            ],
        ]
        ids = {v.offer_id for v in item_views}
        if ids:
            item_rows = (
                (await session.execute(select(candidates).where(candidates.c.offer_id.in_(ids))))
                .mappings()
                .all()
            )
            annotations = {r["offer_id"]: r for r in item_rows}
            for view in item_views:
                if view.offer_id in annotations:
                    annotated = self.annotate(view, annotations[view.offer_id])
                    for key in (
                        "delivered_total",
                        "shipping_price",
                        "tax_status",
                        "additional_tax",
                        "tax_included",
                        "delivery_quote_status",
                        "delivery_quote_at",
                        "delivery_quote_expires_at",
                        "delivery_availability",
                    ):
                        setattr(view, key, getattr(annotated, key))
