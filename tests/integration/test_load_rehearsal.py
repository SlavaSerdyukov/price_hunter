import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select, update

from pricehunter.api.app import create_app
from pricehunter.core.container import Container
from pricehunter.db.base import utcnow
from pricehunter.db.models import StoreOffer
from pricehunter.db.session import create_sessions
from pricehunter.operations.integrity import integrity, state
from pricehunter.operations.load import run_load
from pricehunter.operations.synthetic import seed

pytestmark = pytest.mark.integration


async def test_bounded_pool_pressure_safe_error_and_no_leak(container):
    dataset = await seed(container)
    settings = container.settings.model_copy(deep=True)
    settings.awin_enabled = False
    settings.database_url = SecretStr(os.environ["TEST_DATABASE_URL"])
    settings.database_pool_size = 1
    settings.database_max_overflow = 0
    settings.database_pool_timeout_seconds = 1
    sessions = create_sessions(settings)
    resources = Container(settings, sessions=sessions, redis=container.redis)
    engine = sessions.kw["bind"]
    app = create_app(settings, resources)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + dataset.api_keys[2]},
        ) as client:
            async with engine.connect():
                async with asyncio.timeout(5):
                    responses = await asyncio.gather(
                        *(client.get("/api/v1/users/me/settings") for _ in range(4))
                    )
                assert all(
                    r.status_code == 503 and r.json() == {"error": "service_unavailable"}
                    for r in responses
                )
            assert (await client.get("/api/v1/users/me/settings")).status_code == 200
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        await resources.close()
        await engine.dispose()


async def test_concurrent_shared_budget_isolates_abusive_user_and_has_ttl(container):
    dataset = await seed(container)
    container.settings.user_requests_per_minute = 3
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:

        async def request(index):
            return await client.get(
                "/api/v1/users/me/settings",
                headers={"Authorization": "Bearer " + dataset.api_keys[index]},
            )

        responses = await asyncio.gather(*(request(3) for _ in range(20)), request(4), request(4))
    assert sum(r.status_code == 200 for r in responses[:20]) == 3
    assert sum(r.status_code == 429 for r in responses[:20]) == 17
    assert all(r.status_code == 200 for r in responses[20:])
    for index in (3, 4):
        assert 0 < await container.redis.ttl(f"ph:rate:user:{dataset.user_ids[index]}") <= 60
    await integrity(container.sessions)


async def test_http_reads_with_background_workers_and_many_stored_offers(container, monkeypatch):
    dataset = await seed(container)
    async with container.sessions.begin() as session:
        table = StoreOffer.__table__
        template = dict(
            (await session.execute(select(table).where(table.c.id == dataset.offer_id)))
            .mappings()
            .one()
        )
        await session.execute(
            insert(table),
            [template | {"id": uuid4(), "external_id": f"extra-{i}"} for i in range(500)],
        )
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == dataset.offer_id)
            .values(
                next_check_at=utcnow() - timedelta(seconds=1),
                last_checked_at=utcnow() - timedelta(minutes=2),
            )
        )
    quotes = AsyncMock(side_effect=AssertionError("Persisted comparison must not quote"))
    monkeypatch.setattr(container.registry.get("mock"), "quote_delivery", quotes)
    before = await state(container.sessions)
    app = create_app(container.settings, container)
    claims = 0

    async def maintenance():
        nonlocal claims
        for _ in range(4):
            async with container.sessions.begin() as session:
                await container.watches.evaluate(session, dataset.product_id, market_country="DE")
            claims += len(await container.price_checks.claim_due())
            await asyncio.sleep(0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        report, _ = await asyncio.gather(
            run_load(container, dataset, client, "smoke"), maintenance()
        )
    assert report.unexpected_failures == 0 and report.response_bounds_passed
    assert claims >= 1
    assert report.checked_out_connections == 0
    quotes.assert_not_awaited()
    after = await state(container.sessions)
    for name in (
        "payment_events",
        "notification_events",
        "best_price_events",
        "price_observations",
    ):
        assert after[name] == before[name]
    await integrity(container.sessions)


async def test_explicit_mock_delivery_global_bound(container, monkeypatch):
    from pricehunter.domain.delivery import DeliveryContext

    dataset = await seed(container)
    provider = container.registry.get("mock")
    quote = AsyncMock(wraps=provider.quote_delivery)
    monkeypatch.setattr(provider, "quote_delivery", quote)
    container.settings.delivery_quote_limit = 1
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.product_id == dataset.product_id)
            .values(last_checked_at=utcnow() - timedelta(seconds=1))
        )
    await container.delivery.request(
        dataset.product_id, dataset.user_ids[2], DeliveryContext(country="NL"), market_country="BE"
    )
    assert quote.await_count == 1
    await integrity(container.sessions)


async def test_search_provider_persistence_shortlist_and_entitlement_bounds(container, monkeypatch):
    from pricehunter.providers.mock import MockStoreProvider

    dataset = await seed(container)
    provider = container.registry.get("mock")
    offers = [
        MockStoreProvider()
        ._offer("sony-a")
        .model_copy(
            update={
                "external_id": f"bound-{i}",
                "variant": {"color": f"synthetic-{i}"},
            }
        )
        for i in range(250)
    ]
    monkeypatch.setattr(provider, "search", AsyncMock(return_value=offers))
    container.registry.providers = {"mock": provider}
    settings = container.settings
    settings.search_provider_candidate_limit = 7
    settings.search_persistence_limit = 3
    settings.search_comparison_limit = 2
    persist = AsyncMock(wraps=container.products.persist)
    compare = AsyncMock(wraps=container.products.product)
    monkeypatch.setattr(container.products, "persist", persist)
    monkeypatch.setattr(container.products, "product", compare)
    results = await asyncio.gather(
        *(container.search.search("Sony", user, country="DE") for user in dataset.user_ids[3:7])
    )
    assert persist.await_count == 4 * 3
    assert all(len(result.products) == 2 for result in results)
    assert all(result.provider_outcomes[0].result_count == 250 for result in results)
    assert all("candidate_limit" in result.provider_outcomes[0].errors for result in results)
    assert all("persistence_limit" in result.provider_outcomes[0].errors for result in results)
    assert compare.await_count == 4 * 2
    # Keep all acquisition/build caps larger than this customer's result entitlement.
    from dataclasses import replace

    from pricehunter.domain.subscriptions import Plan

    container.policy.limits[Plan.FREE] = replace(
        container.policy.for_plan(Plan.FREE), search_result_limit=1
    )
    free = await container.search.search("Sony", dataset.user_ids[0], country="DE")
    assert len(free.products) == 1
    await integrity(container.sessions)


async def test_real_tcp_operator_smoke(sessions, monkeypatch):
    from urllib.parse import urlsplit, urlunsplit

    from pricehunter.operations.rehearsal import http_rehearsal

    source = os.environ["TEST_DATABASE_URL"]
    redis = urlsplit(os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("REHEARSAL_DATABASE_URL", source)
    monkeypatch.setenv("REHEARSAL_REDIS_URL", urlunsplit(redis._replace(path="/14")))
    report = (await http_rehearsal("smoke"))["load"]
    assert report["requests"] == 56 and report["unexpected_failures"] == 0
    assert report["checked_out_connections"] == 0 and report["response_bounds_passed"]
