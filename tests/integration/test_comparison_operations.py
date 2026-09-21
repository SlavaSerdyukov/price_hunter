from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, insert, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    PriceObservation,
    Product,
    ProductBestState,
    Store,
    StoreOffer,
)
from pricehunter.domain.errors import ProductNotFoundError, RateLimitExceededError
from pricehunter.domain.freshness import Freshness
from pricehunter.jobs.worker import maintain_comparisons
from pricehunter.services.catalog_diagnostics import CatalogDiagnostics
from pricehunter.services.comparison_service import ComparisonReader
from tests.integration.test_api import api_client
from tests.integration.test_comparison import accept_price, event_types, persist, setup_watch
from tests.support import grant_plan

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "provider,ttl",
    [
        ("mock", 300),
        ("woocommerce_be", 900),
        ("woocommerce_fr", 500),
        ("woocommerce_be_more", 500),
        ("other", 60),
        ("amazon", 0),
    ],
)
async def test_sql_freshness_matches_policy_and_boundary(container, provider, ttl):
    a = await persist(container, "sony-a")
    settings = container.settings.model_copy(
        update={
            "offer_freshness_seconds": {
                "default": 60,
                "mock": 300,
                "woocommerce": 500,
                "woocommerce_be": 900,
                "amazon": 0,
            }
        }
    )
    reader = ComparisonReader(settings)
    now = utcnow()
    async with container.sessions.begin() as session:
        row = await session.get(StoreOffer, a.id)
        store = await session.get(Store, row.store_id)
        store.provider_type = provider
        for age, failures, quarantine in (
            (ttl - 1, 0, None),
            (ttl, 0, None),
            (0, 1, None),
            (0, 0, Decimal(1)),
            (-61, 0, None),
        ):
            row.last_checked_at = now - timedelta(seconds=age)
            row.failure_count, row.suspicious_price = failures, quarantine
            await session.flush()
            actual = await session.scalar(
                select(reader.freshness_expression(now)).select_from(StoreOffer).join(Store)
            )
            assert actual == reader.view(row, store, now).freshness
            assert reader.view(row, store, now).age_seconds >= 0
        default_reader = ComparisonReader(
            settings.model_copy(update={"offer_freshness_seconds": {"default": 60}})
        )
        assert await session.scalar(select(default_reader.ttl_expression())) == 60


async def test_stale_failed_and_currency_groups_never_supply_current_best(container):
    user, a, b, _ = await setup_watch(container)
    usd = await persist(container, "sony-us")
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(last_checked_at=utcnow() - timedelta(days=4), price=299)
        )
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == b.id).values(failure_count=1)
        )
    product = await container.products.product(a.product_id, user.id)
    groups = {g.currency: g for g in product.currency_groups}
    assert product.best_available_offer is None
    assert groups["EUR"].best_available_offer is None and groups["EUR"].price_spread is None
    assert groups["EUR"].cheapest_stale_offer.price == 299
    assert (groups["EUR"].fresh_offer_count, groups["EUR"].failed_offer_count) == (0, 1)
    assert groups["USD"].best_available_offer.offer_id == usd.id
    assert {o.freshness for o in product.offers} == {
        Freshness.FRESH,
        Freshness.STALE,
        Freshness.FAILED,
    }


async def test_expiry_sweep_and_refreshed_prices_are_not_restock_alerts(container):
    user, a, _, _ = await setup_watch(container)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).values(last_checked_at=utcnow() - timedelta(hours=1))
        )
        await session.execute(
            update(ProductBestState).values(next_evaluation_at=utcnow() - timedelta(seconds=1))
        )
    assert await maintain_comparisons({"container": container}) == 1
    assert await maintain_comparisons({"container": container}) == 0
    assert (await container.watches.list(user.id))[0].best_offer_id is None
    assert await event_types(container) == []
    await accept_price(container, a, "sony-a", "329")
    assert await event_types(container) == ["best_prices_refreshed"]
    async with container.sessions() as session:
        assert list(
            await session.scalars(
                select(BestPriceEvent.event_type).order_by(BestPriceEvent.sequence)
            )
        ) == ["initial_best", "became_unavailable", "restored"]


@pytest.mark.parametrize("evidence", ["unknown", "stale_in_stock"])
async def test_uncertain_stock_does_not_become_a_restock_claim(container, evidence):
    user, a, b, _ = await setup_watch(container)
    await grant_plan(container, user.id)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == a.id).values(availability="out_of_stock")
        )
        row = await session.get(StoreOffer, b.id)
        if evidence == "unknown":
            row.availability = "unknown"
        else:
            row.last_checked_at = utcnow() - timedelta(hours=1)
        await session.get(Product, a.product_id, with_for_update=True)
        await session.flush()
        await container.watches.evaluate(session, a.product_id)
    await accept_price(container, a, "sony-a", "320")
    assert await event_types(container) == ["best_prices_refreshed"]


async def test_legacy_watch_backfill_is_idempotent_and_failure_removes_recommendation(container):
    user, a, b, _ = await setup_watch(container)
    async with container.sessions.begin() as session:
        await session.execute(delete(BestPriceEvent))
        await session.execute(delete(ProductBestState))
    assert await container.comparison_operations.maintain() == 1
    assert await container.comparison_operations.maintain() == 0
    assert await event_types(container) == []
    claim = next(c for c in await container.price_checks.claim_due() if c.offer_id == a.id)
    await container.price_checks.failed(claim)
    assert (
        await container.products.product(a.product_id, user.id)
    ).best_available_offer.offer_id == b.id
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BestPriceEvent)) == 2


async def test_bounded_manual_refresh_uses_existing_scheduler_without_watch(container):
    user = await container.users.telegram(111)
    a = await persist(container, "sony-a")
    b = await persist(container, "sony-b")
    container.settings.comparison_refresh_limit = 1
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).values(
                last_checked_at=utcnow() - timedelta(hours=1),
                next_check_at=utcnow() + timedelta(days=1),
            )
        )
    assert await container.price_checks.claim_due() == []
    provider = container.registry.get("mock")
    provider.refresh_offer = AsyncMock(wraps=provider.refresh_offer)
    result = await container.comparison_operations.request_refresh(a.product_id, user.id)
    assert result.accepted and result.queued_count == 1
    provider.refresh_offer.assert_not_called()
    (claim,) = await container.price_checks.claim_due()
    assert claim.offer_id in (a.id, b.id)
    assert await container.price_checks.refresh(claim)
    provider.refresh_offer.assert_awaited_once()
    assert await container.price_checks.claim_due() == []
    with pytest.raises(RateLimitExceededError):
        await container.comparison_operations.request_refresh(a.product_id, user.id)
    async with container.sessions() as session:
        assert (await session.get(StoreOffer, claim.offer_id)).refresh_requested_at is None


async def test_refresh_api_history_limits_unknown_products_and_auth(container):
    client = await api_client(container, 111)
    a = await persist(container, "sony-a")
    async with client:
        root = f"/api/v1/products/{a.product_id}"
        history = await client.get(
            root + "/best-price-history", params={"currency": "EUR", "limit": 1}
        )
        assert history.status_code == 200 and len(history.json()["points"]) == 1
        assert history.json()["retention_days"] == 7
        for params in ({"currency": "eur"}, {"currency": "EUR", "limit": 201}, {}):
            assert (
                await client.get(root + "/best-price-history", params=params)
            ).status_code == 422
        assert (
            await client.get(
                root + "/best-price-history",
                params={"currency": "EUR"},
                headers={"Authorization": ""},
            )
        ).status_code == 401
        assert (
            await client.get(
                f"/api/v1/products/{uuid4()}/best-price-history", params={"currency": "EUR"}
            )
        ).status_code == 404
        assert (await client.post(f"/api/v1/products/{uuid4()}/refresh")).status_code == 404
        reply = await client.post(root + "/refresh")
        assert reply.status_code == 202 and reply.json() == {"accepted": True, "queued_count": 0}
        assert (await client.post(root + "/refresh")).status_code == 429


async def test_history_per_currency_bounds_plan_duration_and_retention_anchor(container):
    user, a, _, _ = await setup_watch(container)
    await persist(container, "sony-us")
    await accept_price(container, a, "sony-a", "320")
    await accept_price(container, a, "sony-a", "310")
    now = utcnow()
    async with container.sessions.begin() as session:
        eur = list(
            await session.scalars(
                select(BestPriceEvent)
                .where(BestPriceEvent.currency == "EUR")
                .order_by(BestPriceEvent.sequence)
            )
        )
        for e, days in zip(eur, [40, 20, 2], strict=True):
            e.observed_at = now - timedelta(days=days)
    free = await container.best_prices.history(a.product_id, user.id, "EUR")
    assert free.retention_days == 7 and free.minimum == 310
    assert [p.event_type for p in free.points] == ["window_start", "price_changed"]
    assert free.points[0].price == 320
    limited = await container.best_prices.history(a.product_id, user.id, "EUR", limit=1)
    assert len(limited.points) == 1 and limited.points[0].price == 310
    await grant_plan(container, user.id)
    paid = await container.best_prices.history(a.product_id, user.id, "EUR")
    assert paid.retention_days == 90 and len(paid.points) == 3
    usd = await container.best_prices.history(a.product_id, user.id, "USD")
    assert len(usd.points) == 1 and usd.minimum == 80
    empty = await container.best_prices.history(a.product_id, user.id, "JPY")
    assert empty.current_best is None and empty.points == [] and empty.minimum is None
    container.settings.history_retention_days = 7
    assert await container.best_prices.prune() == 1
    assert await container.best_prices.prune() == 0
    async with container.sessions.begin() as session:
        state = await session.scalar(
            select(ProductBestState).where(ProductBestState.currency == "EUR")
        )
        assert state.price == 310 and state.sequence == 3
        await session.execute(delete(PriceObservation))
    async with container.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BestPriceEvent)
                .where(BestPriceEvent.source_observation_id.is_not(None))
            )
            == 0
        )
    assert (await container.best_prices.history(a.product_id, user.id, "EUR")).retention_days == 7
    container.settings.history_retention_days = 0
    assert await container.best_prices.prune() == 0


async def test_catalog_diagnostics_and_duplicates_are_read_only(container):
    _, a, _, _ = await setup_watch(container)
    white = await persist(container, "sony-white")
    diagnostics = CatalogDiagnostics(container.sessions, container.settings)
    report = await diagnostics.product(a.product_id)
    assert report["counts"] == {
        "watches": 1,
        "best_price_events": 1,
        "discoveries": 0,
        "offers": 2,
        "trackers": 0,
        "observations": 2,
    }
    assert all(o["freshness"] == "fresh" and o["match"]["matched"] for o in report["offers"])
    conflicts = await diagnostics.product(white.product_id, a.product_id)
    assert conflicts["offers"][0]["match"]["method"] == "variant_mismatch"
    candidates = await diagnostics.duplicates()
    assert candidates and all(c["requires_review"] for c in candidates)
    assert {a.product_id, white.product_id} <= set(candidates[0]["product_ids"])
    with pytest.raises(ProductNotFoundError):
        await diagnostics.product(uuid4())
    with pytest.raises(ProductNotFoundError):
        await diagnostics.product(a.product_id, uuid4())
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 2
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 3


@pytest.mark.parametrize("size", [100, 500, 1000])
async def test_large_comparison_has_bounded_queries_and_pagination(container, size):
    a = await persist(container, "sony-a")
    now = utcnow()
    async with container.sessions.begin() as session:
        stored = await session.get(StoreOffer, a.id)
        await session.execute(
            insert(StoreOffer),
            [
                {
                    "product_id": a.product_id,
                    "store_id": stored.store_id,
                    "external_id": f"load_{i}",
                    "url": a.url,
                    "direct_url": stored.direct_url,
                    "title": "Load fixture",
                    "price": i + 1,
                    "currency": "EUR",
                    "availability": "in_stock",
                    "minimum_price": i + 1,
                    "maximum_price": i + 1,
                    "total_price": i + 1,
                    "last_checked_at": now - timedelta(hours=1) if i % 2 == 0 else now,
                }
                for i in range(size - 1)
            ],
        )
    statements = []
    engine = container.sessions.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with container.sessions() as session:
            reader = container.products.comparisons
            first = await reader.build(session, a.product_id, size=10)
            assert len(statements) == 6
            assert first.offer_count == size and len(first.offers) == 10
            assert first.best_available_offer.price == 2
            assert first.currency_groups[0].cheapest_stale_offer.price == 1
            assert first.currency_groups[0].stale_offer_count == size // 2
            second = await reader.build(session, a.product_id, page=1, size=10)
            assert len(statements) <= 12
            assert not {o.offer_id for o in first.offers} & {o.offer_id for o in second.offers}
            assert second.currency_groups == first.currency_groups
            last = await reader.build(session, a.product_id, page=size // 10 - 1, size=10)
            assert len(last.offers) == 10 and last.offer_count == size
    finally:
        event.remove(engine, "before_cursor_execute", capture)
