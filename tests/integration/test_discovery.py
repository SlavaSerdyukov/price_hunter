import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    NotificationEvent,
    PriceObservation,
    Product,
    ProductDiscovery,
    ProductWatch,
    StoreOffer,
    Subscription,
    User,
)
from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.errors import RateLimitExceededError
from pricehunter.jobs.worker import discover_product, discover_products
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.watches import WatchCreate, WatchPatch
from pricehunter.services.discovery_service import DiscoveryClaim
from tests.integration.test_comparison import accept_price, event_types, persist, setup_watch
from tests.support import grant_plan

pytestmark = pytest.mark.integration


async def counts(container):
    async with container.sessions() as session:
        return [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Product, StoreOffer, PriceObservation, BestPriceEvent, NotificationEvent)
        ]


async def force_discovery_due(container):
    async with container.sessions.begin() as session:
        await session.execute(
            update(ProductDiscovery).values(
                next_discovery_at=utcnow() - timedelta(days=8),
                last_success_at=utcnow() - timedelta(days=8),
                lease_until=None,
            )
        )


async def test_mock_autonomous_acceptance_and_replay(container):
    # Persist the old listing as old data: never insert it as a current observation.
    user, a, b, watch = await setup_watch(container)
    async with container.sessions.begin() as session:
        old = await container.products.resolver.resolve(
            session, MockStoreProvider()._offer("sony-old")
        )
        old.last_checked_at = utcnow() - timedelta(days=4)
    before = await container.products.product(a.product_id, user.id)
    assert before.best_available_offer.offer_id == a.id
    assert before.currency_groups[0].cheapest_stale_offer.price == 299
    assert before.currency_groups[0].stale_offer_count == 1
    assert await event_types(container) == []
    assert await container.discovery.synchronize() == 1
    (claim,) = await container.discovery.claim_due()
    assert await container.discovery.discover(claim)
    after = await container.products.product(a.product_id, user.id)
    new_id = after.best_available_offer.offer_id
    assert after.best_available_offer.price == 315
    assert after.discovery[0].status == "ok"
    assert await event_types(container) == ["merchant_became_cheapest"]
    snapshot = await counts(container)
    assert not await container.discovery.discover(claim)
    await force_discovery_due(container)
    (replay,) = await container.discovery.claim_due()
    assert await container.discovery.discover(replay)
    assert await counts(container) == snapshot
    async with container.sessions.begin() as session:
        new = await session.get(StoreOffer, new_id)
        new.last_checked_at = utcnow() - timedelta(hours=1)
    await accept_price(container, a, "sony-a", "320")
    assert (
        await container.products.product(a.product_id, user.id)
    ).best_available_offer.offer_id == a.id
    await accept_price(container, new, "sony-new", "310")
    final = await container.products.product(a.product_id, user.id)
    assert final.best_available_offer.price == 310 and final.best_available_offer.offer_id == new_id
    assert await event_types(container) == ["merchant_became_cheapest"] * 3
    async with container.sessions() as session:
        events = list(
            await session.scalars(select(BestPriceEvent).order_by(BestPriceEvent.sequence))
        )
        assert [e.price for e in events] == [329, 315, 320, 310]
        assert len({e.sequence for e in events}) == 4
        assert events[-1].source_observation_id is not None


async def test_hundred_watchers_share_one_target_and_concurrent_lease(container):
    _, a, _, _ = await setup_watch(container)
    async with container.sessions.begin() as session:
        for i in range(99):
            user = User(id=uuid4(), telegram_user_id=1000 + i, country_code="BE")
            session.add(user)
            await session.flush()
            session.add(
                ProductWatch(
                    market_country="BE",
                    user_id=user.id,
                    product_id=a.product_id,
                    currency="EUR",
                    best_offer_id=a.id,
                    best_price=a.price,
                )
            )
    assert sum(await asyncio.gather(*(container.discovery.synchronize() for _ in range(3)))) == 1
    claims = sum(await asyncio.gather(*(container.discovery.claim_due() for _ in range(4))), [])
    assert len(claims) == 1
    provider = container.registry.get("mock")
    provider.discover = AsyncMock(wraps=provider.discover)
    assert (
        sum(await asyncio.gather(*(container.discovery.discover(claims[0]) for _ in range(4)))) == 1
    )
    assert provider.discover.await_count == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProductDiscovery)) == 1
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 100
        assert await session.scalar(select(func.count()).select_from(BestPriceEvent)) == 2


async def test_fencing_expired_claims_paused_and_disabled_targets(container):
    user, _, _, watch = await setup_watch(container)
    await container.discovery.synchronize()
    (old,) = await container.discovery.claim_due()
    async with container.sessions.begin() as session:
        await session.execute(
            update(ProductDiscovery).values(lease_until=utcnow() - timedelta(seconds=1))
        )
    assert not await container.discovery.discover(old)
    await container.discovery.failed(old, "expired_failure")
    async with container.sessions() as session:
        assert (await session.get(ProductDiscovery, old.target_id)).failure_count == 0
    (current,) = await container.discovery.claim_due()
    assert current.token != old.token
    query = DiscoveryQuery("gtin", "4006381333931", "BE", "EUR")
    data = [MockStoreProvider()._offer("sony-new")]
    assert not await container.discovery.accept(old, data, query, 600)
    await container.discovery.failed(old, "late_failure")
    async with container.sessions() as session:
        target = await session.get(ProductDiscovery, current.target_id)
        assert target.lease_token == current.token and target.failure_count == 0
    await container.watches.update(user.id, watch.id, WatchPatch(enabled=False))
    assert not await container.discovery.accept(current, data, query, 600)
    assert await container.discovery.claim_due() == []
    await container.watches.update(user.id, watch.id, WatchPatch(enabled=True))
    container.settings.discovery_enabled = False
    assert await container.discovery.synchronize() == 0
    assert await container.discovery.claim_due() == []
    assert not await container.discovery.discover(current)


async def test_empty_and_failed_providers_are_distinct_and_isolated(container):
    user, a, _, _ = await setup_watch(container)

    class Failed(MockStoreProvider):
        name = "failed"

        async def discover(self, query):
            raise TimeoutError()

    class Empty(MockStoreProvider):
        name = "empty"

        async def discover(self, query):
            return []

    container.registry.providers.update(failed=Failed(), empty=Empty())
    container.settings.discovery_failure_threshold = 1
    await container.discovery.synchronize()
    claims = await container.discovery.claim_due()
    assert sorted(await asyncio.gather(*(container.discovery.discover(c) for c in claims))) == [
        False,
        True,
        True,
    ]
    product = await container.products.product(a.product_id, user.id)
    statuses = {d.provider: d for d in product.discovery}
    assert statuses["empty"].status == "empty"
    assert statuses["failed"].status == "backed_off"
    assert statuses["failed"].last_success_at is None
    assert product.offer_count == 3 and product.best_available_offer.price == 315
    assert await container.redis.exists("ph:discovery:circuit:failed")
    await force_discovery_due(container)
    claims = await container.discovery.claim_due()
    await asyncio.gather(*(container.discovery.discover(c) for c in claims))
    product = await container.products.product(a.product_id, user.id)
    assert (
        next(d for d in product.discovery if d.provider == "failed").error_code
        == "provider_suppressed"
    )


async def test_rate_limit_and_persistence_failure_retry_without_poisoning_circuit(
    container, monkeypatch
):
    await setup_watch(container)
    await container.discovery.synchronize()
    provider = container.registry.get("mock")
    original = provider.discover
    provider.discover = AsyncMock(side_effect=RateLimitExceededError())
    (claim,) = await container.discovery.claim_due()
    assert not await container.discovery.discover(claim)
    assert not await container.redis.exists("ph:discovery:failures:mock")
    async with container.sessions() as session:
        target = await session.get(ProductDiscovery, claim.target_id)
        assert target.last_error_code == "rate_limited" and target.next_discovery_at > utcnow()
    assert await container.discovery.claim_due() == []
    await force_discovery_due(container)
    provider.discover = original
    monkeypatch.setattr(
        container.discovery.resolver, "resolve", AsyncMock(side_effect=RuntimeError())
    )
    (claim,) = await container.discovery.claim_due()
    assert not await container.discovery.discover(claim)
    async with container.sessions() as session:
        target = await session.get(ProductDiscovery, claim.target_id)
        assert target.last_error_code == "persistence_failed"
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 2


async def test_only_matching_results_attach_and_missing_metadata_cannot_bridge_conflicts(container):
    _, a, _, _ = await setup_watch(container)
    await container.discovery.synchronize()
    (claim,) = await container.discovery.claim_due()
    p = MockStoreProvider()
    good = p._offer("sony-new").model_copy(update={"variant": {}})
    wrong_provider = good.model_copy(update={"provider": "unrelated"})
    wrong_currency = good.model_copy(update={"currency": "USD"})
    assert await container.discovery.accept(
        claim,
        [
            p._offer("sony-white"),
            p._offer("sony-conflict"),
            wrong_provider,
            wrong_currency,
            good,
            good,
        ],
        DiscoveryQuery("gtin", "4006381333931", "BE", "EUR"),
        600,
    )
    assert (await counts(container))[:3] == [1, 3, 3]
    # The black evidence remains even after attaching an offer without a color.
    white = await persist(container, "sony-white")
    assert white.product_id != a.product_id


async def test_discovery_contexts_fastest_plan_and_expired_subscription(container):
    user, _, _, _ = await setup_watch(container)
    provider = container.registry.get("mock")
    provider.discovery_interval_seconds = None
    await container.discovery.synchronize()
    (claim,) = await container.discovery.claim_due()
    assert await container.discovery.discover(claim)
    async with container.sessions.begin() as session:
        await session.execute(
            update(ProductDiscovery).values(last_success_at=utcnow() - timedelta(days=2))
        )
    assert await container.discovery.claim_due() == []
    await grant_plan(container, user.id)
    (claim,) = await container.discovery.claim_due()  # Paid plan advances an old Free schedule.
    assert await container.discovery.discover(claim)
    async with container.sessions.begin() as session:
        await session.execute(
            update(Subscription).values(valid_until=utcnow() - timedelta(seconds=1))
        )
        await session.execute(
            update(ProductDiscovery).values(
                last_success_at=utcnow() - timedelta(days=2),
                next_discovery_at=utcnow() - timedelta(seconds=1),
            )
        )
    assert await container.discovery.claim_due() == []  # Downgrade restores slower shared cadence.


async def test_unsupported_identity_market_and_removed_provider(container):
    user = await container.users.telegram(111)
    data = (
        MockStoreProvider()._offer("headphones").model_copy(update={"brand": None, "model": None})
    )
    a = await container.products.persist(data)
    await container.watches.create(
        user.id, WatchCreate(market_country="BE", product_id=a.product_id, currency="EUR")
    )
    p = container.registry.get("mock")
    p.discovery_countries = frozenset({"US"})
    assert await container.discovery.synchronize() == 0
    p.discovery_countries = frozenset()
    p.capabilities = frozenset({Capability.SEARCH_KEYWORD})
    assert await container.discovery.synchronize() == 0
    p.capabilities = MockStoreProvider.capabilities
    assert await container.discovery.synchronize() == 1
    (claim,) = await container.discovery.claim_due()
    assert not await container.discovery.discover(claim)
    assert (await container.products.product(a.product_id, user.id)).discovery[
        0
    ].status == "unsupported"
    await force_discovery_due(container)
    (claim,) = await container.discovery.claim_due()
    container.registry.providers.clear()
    assert await container.discovery.claim_due() == []
    assert not await container.discovery.discover(claim)


async def test_lost_enqueue_lease_recovers_and_worker_delegates(container):
    await setup_watch(container)
    queue = AsyncMock()
    queue.enqueue_job.side_effect = ConnectionError()
    assert await discover_products({"container": container, "redis": queue}) == 1
    assert await discover_products({"container": container, "redis": queue}) == 0
    async with container.sessions.begin() as session:
        await session.execute(
            update(ProductDiscovery).values(lease_until=utcnow() - timedelta(seconds=1))
        )
    queue.enqueue_job.side_effect = None
    assert await discover_products({"container": container, "redis": queue}) == 1
    _, target, token = queue.enqueue_job.call_args.args
    assert await discover_product({"container": container}, target, token)
    assert not await container.discovery.discover(DiscoveryClaim(uuid4(), uuid4()))


async def test_network_uses_no_database_connection_and_late_batch_rolls_back(
    container, monkeypatch
):
    await setup_watch(container)
    await container.discovery.synchronize()
    (claim,) = await container.discovery.claim_due()
    baseline = await counts(container)
    provider = container.registry.get("mock")

    async def network(query):
        assert container.sessions.kw["bind"].pool.checkedout() == 0
        return [provider._offer("sony-new")]

    provider.discover = network
    evaluate = container.discovery.watches.evaluate

    async def slow_evaluation(*args):
        await evaluate(*args)
        # Simulate the clock crossing the deadline during persistence, after history/outbox.
        late = utcnow() + timedelta(seconds=container.settings.discovery_lease_seconds + 1)
        monkeypatch.setattr("pricehunter.services.discovery_service.utcnow", lambda: late)

    monkeypatch.setattr(container.discovery.watches, "evaluate", slow_evaluation)
    assert not await container.discovery.discover(claim)
    assert await counts(container) == baseline


async def test_result_bound_and_context_validation(container):
    await setup_watch(container)
    await container.discovery.synchronize()
    (claim,) = await container.discovery.claim_due()
    data = MockStoreProvider()._offer("sony-new")
    with pytest.raises(ValueError, match="context"):
        await container.discovery.accept(
            claim, [data], DiscoveryQuery("gtin", "x", "DE", "EUR"), 600
        )
    container.settings.discovery_result_limit = 1
    different = data.model_copy(update={"external_id": "more", "store_slug": "more"})
    assert await container.discovery.accept(
        claim, [data, different], DiscoveryQuery("gtin", "x", "BE", "EUR"), 600
    )
    assert (await counts(container))[:3] == [1, 3, 3]


async def test_summary_search_enrichment_is_bounded_limited_and_outside_transactions(container):
    from pricehunter.domain.errors import ProductNotFoundError

    await setup_watch(container)

    class SummaryProvider(MockStoreProvider):
        name = "summaries"
        capabilities = MockStoreProvider.capabilities | {Capability.SEARCH_DETAILS}
        details = []

        async def discover(self, query):
            weak = self._offer("sony-new").model_copy(
                update={"provider": self.name, "gtin": None, "brand": None, "model": None}
            )
            return [weak.model_copy(update={"external_id": x}) for x in ("ended", "new", "excess")]

        async def refresh_offer(self, reference):
            assert container.sessions.kw["bind"].pool.checkedout() == 0
            self.details.append(reference.external_id)
            if reference.external_id == "ended":
                raise ProductNotFoundError()
            return self._offer("sony-new").model_copy(update={"provider": self.name})

    provider = SummaryProvider()
    container.registry.providers = {provider.name: provider}
    container.settings.discovery_result_limit = 2
    await container.discovery.synchronize()
    (claim,) = await container.discovery.claim_due()
    assert await container.discovery.discover(claim)
    assert provider.details == ["ended", "new"]
    assert int(await container.redis.get("ph:rate:provider:summaries")) == 3
    assert (await counts(container))[:3] == [1, 3, 3]


async def test_ebay_discovery_fetches_actual_identity_instead_of_assuming_query_match(
    container, respx_mock
):
    import json
    from pathlib import Path

    import httpx

    from pricehunter.providers.ebay import EbayBrowseProvider
    from pricehunter.providers.http import ProviderHTTP

    payload = json.loads((Path(__file__).parents[1] / "fixtures/ebay_item.json").read_text())
    async with httpx.AsyncClient() as client:
        adapter = EbayBrowseProvider(
            ProviderHTTP(client, timeout=5, max_bytes=20000), "test-id", "test-secret", ["DE"]
        )
        container.registry.providers = {"ebay": adapter}
        user = await container.users.telegram(111)
        async with container.sessions.begin() as session:
            await session.execute(update(User).values(country_code="DE"))
        a = await container.products.persist(adapter._normalize(payload, "DE"))
        await container.watches.create(
            user.id, WatchCreate(market_country="DE", product_id=a.product_id, currency="EUR")
        )
        new = dict(
            payload,
            itemId="v1|123456789013|0",
            itemWebUrl="https://www.ebay.de/itm/123456789013",
            price={"value": "79.99", "currency": "EUR"},
        )
        summary = {k: v for k, v in new.items() if k not in {"brand", "gtin", "localizedAspects"}}
        respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
            200, json={"access_token": "test-token", "expires_in": 3600}
        )
        search = respx_mock.get("https://api.ebay.com/buy/browse/v1/item_summary/search").respond(
            200, json={"itemSummaries": [summary]}
        )
        details = respx_mock.get(
            "https://api.ebay.com/buy/browse/v1/item/v1%7C123456789013%7C0"
        ).respond(200, json=new)
        await container.discovery.synchronize()
        (claim,) = await container.discovery.claim_due()
        assert await container.discovery.discover(claim)
        assert search.calls[0].request.url.params["gtin"] == "04006381333931"
        assert details.call_count == 1
        product = await container.products.product(a.product_id, user.id)
        assert product.offer_count == 2 and product.best_available_offer.price == Decimal("79.99")
        assert await event_types(container) == ["merchant_became_cheapest"]
