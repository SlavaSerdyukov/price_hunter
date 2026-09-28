import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    NotificationEvent,
    PriceObservation,
    Product,
    ProductIdentifier,
    Store,
    StoreOffer,
)
from pricehunter.domain.errors import (
    FeatureRequiresUpgradeError,
    ProductNotFoundError,
    SubscriptionLimitReachedError,
    UnsupportedStoreError,
)
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.api import TrackerCreate
from pricehunter.schemas.watches import WatchCreate, WatchPatch
from pricehunter.services.notification_service import NotificationService
from tests.integration.test_api import api_client
from tests.support import grant_plan

pytestmark = pytest.mark.integration


async def persist(container, slug):
    return await container.products.persist(MockStoreProvider()._offer(slug))


@pytest.mark.parametrize("slugs", [("sony-a", "sony-b"), ("model-a", "model-b")])
async def test_concurrent_providers_share_canonical_identity(container, slugs):
    offers = await asyncio.gather(*(persist(container, slug) for slug in slugs * 4))
    assert len({o.product_id for o in offers}) == 1
    assert len({o.id for o in offers}) == 2
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 2
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 2
    user = await container.users.telegram(111)
    product = await container.products.product(offers[0].product_id, user.id)
    assert len(product.offers) == 2 and product.store_count == 2


async def test_conflicting_identifiers_variants_and_weak_bridges_stay_separate(container):
    a, conflict, white, model, capacity = [
        await persist(container, slug)
        for slug in (
            "sony-a",
            "sony-conflict",
            "sony-white",
            "model-a",
            "model-256",
        )
    ]
    assert len({o.product_id for o in (a, conflict, white, model, capacity)}) == 5
    weak = MockStoreProvider()._offer("sony-b").model_copy(update={"gtin": None})
    weak_result = await container.products.persist(weak)
    assert weak_result.product_id not in (a.product_id, conflict.product_id)
    # A subsequent strongly identified offer must still find its unambiguous exact match.
    strong = (
        MockStoreProvider()
        ._offer("sony-a")
        .model_copy(update={"store_slug": "new_store", "external_id": "new"})
    )
    assert (await container.products.persist(strong)).product_id == a.product_id


async def test_search_persists_comparisons_and_api_returns_all_stores(container):
    client = await api_client(container, 111)
    async with client:
        result = await client.get("/api/v1/search", params={"q": "Sony WH-1000XM6"})
        assert result.status_code == 200
        products = result.json()["products"]
        product = next(p for p in products if p["store_count"] == 5)
        assert len(products) == 3  # Black, conflicting GTIN, white variant.
        assert product["best_available_offer"] is None and product["price_spread"] is None
        assert product["currencies"] == ["EUR", "USD"]
        eur = product["currency_groups"][0]
        assert eur["best_available_offer"]["store"] == "Demo Alpha"
        assert Decimal(eur["best_available_offer"]["price"]) == 329
        assert Decimal(eur["price_spread"]) == 16
        assert Decimal(eur["cheapest_known_offer"]["price"]) == 90
        loaded = (await client.get(f"/api/v1/products/{product['id']}")).json()
        assert loaded["offer_count"] == 5
        pages = [
            (
                await client.get(
                    f"/api/v1/products/{product['id']}/offers", params={"page": p, "size": 2}
                )
            ).json()
            for p in range(3)
        ]
        assert [len(p["offers"]) for p in pages] == [2, 2, 1]
        assert all(p["currency_groups"] == loaded["currency_groups"] for p in pages)
        assert all(
            o["shipping_price"] is None and o["tax_included"] is None and o["total_price"] is None
            for o in loaded["offers"]
        )


async def test_backfill_preserves_existing_product_and_history(container):
    a = await persist(container, "model-a")
    async with container.sessions.begin() as session:
        await session.execute(delete(ProductIdentifier))
        await session.execute(update(StoreOffer).values(identity_data={}))
        assert await container.products.resolver.backfill(session) == 1
    async with container.sessions.begin() as session:
        assert await container.products.resolver.backfill(session) == 0
    b = await persist(container, "model-b")
    assert b.product_id == a.product_id
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 2


async def test_inactive_store_excluded_and_cannot_be_resolved(container):
    a, b = [await persist(container, slug) for slug in ("sony-a", "sony-b")]
    async with container.sessions.begin() as session:
        await session.execute(update(Store).where(Store.name == "Demo Alpha").values(active=False))
    user = await container.users.telegram(111)
    product = await container.products.product(a.product_id, user.id)
    assert product.best_available_offer.offer_id == b.id
    with pytest.raises(UnsupportedStoreError):
        await persist(container, "sony-a")


async def setup_watch(container):
    user = await container.users.telegram(111)
    a, b = [await persist(container, slug) for slug in ("sony-a", "sony-b")]
    watch = await container.watches.create(
        user.id, WatchCreate(market_country="BE", product_id=a.product_id, currency="EUR")
    )
    return user, a, b, watch


async def make_due(container, offer_id):
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == offer_id)
            .values(
                next_check_at=utcnow() - timedelta(days=1),
                last_checked_at=utcnow() - timedelta(days=1),
                lease_until=utcnow() - timedelta(seconds=1),
            )
        )


async def test_watch_switches_merchant_once_and_uses_existing_outbox(container):
    user, a, b, watch = await setup_watch(container)
    claims = await container.price_checks.claim_due()
    assert {c.offer_id for c in claims} == {a.id, b.id}
    beta = next(c for c in claims if c.offer_id == b.id)
    assert await container.price_checks.refresh(beta)
    assert not await container.price_checks.refresh(beta)
    current = (await container.watches.list(user.id))[0]
    assert current.best_offer_id == b.id and current.best_price == 319
    async with container.sessions() as session:
        events = list(await session.scalars(select(NotificationEvent)))
        assert len(events) == 1 and events[0].event_type == "merchant_became_cheapest"
        assert events[0].product_watch_id == watch.id and events[0].tracker_id is None
        assert events[0].snapshot["previous_price"] == "329.0000"

    class Sender:
        deliveries = []

        async def send(self, delivery):
            self.deliveries.append(delivery)
            return 123

    sender = Sender()
    service = NotificationService(container.sessions, sender, container.entitlements)
    assert await service.send_pending() == 1 and await service.send_pending() == 0
    assert sender.deliveries[0].product_id == a.product_id
    assert sender.deliveries[0].store == "Demo Beta"
    assert sender.deliveries[0].previous_price == 329


async def test_watch_currency_and_stock_guardrails(container):
    user, a, b, watch = await setup_watch(container)
    await persist(container, "sony-us")
    await persist(container, "sony-out")
    await persist(container, "sony-unknown")
    claims = await container.price_checks.claim_due()
    assert len(claims) == 4  # USD is not scheduled by the EUR watch.
    current = (await container.watches.list(user.id))[0]
    assert current.best_offer_id == a.id and current.best_price == 329
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0
    with pytest.raises(ProductNotFoundError):
        await container.watches.create(
            user.id, WatchCreate(market_country="BE", product_id=a.product_id, currency="JPY")
        )


async def test_watch_and_exact_trackers_share_quota_and_api_ownership(container):
    user, a, b, watch = await setup_watch(container)
    tracker = await container.trackers.create(user.id, TrackerCreate(store_offer_id=a.id))
    with pytest.raises(SubscriptionLimitReachedError):
        await container.trackers.create(user.id, TrackerCreate(store_offer_id=b.id))
    other = await container.users.telegram(222)
    with pytest.raises(ProductNotFoundError):
        await container.watches.update(other.id, watch.id, WatchPatch(enabled=False))
    await container.watches.delete(other.id, watch.id)
    assert len(await container.watches.list(user.id)) == 1
    await container.watches.update(user.id, watch.id, WatchPatch(enabled=False))
    assert not (await container.watches.list(user.id))[0].scheduled
    await container.watches.update(user.id, watch.id, WatchPatch(enabled=True))
    assert (await container.watches.list(user.id))[0].scheduled
    with pytest.raises(FeatureRequiresUpgradeError):
        await container.watches.update(user.id, watch.id, WatchPatch(target_price=Decimal(300)))
    await container.watches.delete(user.id, watch.id)
    await container.watches.delete(user.id, watch.id)
    assert await container.watches.list(user.id) == []
    assert (await container.subscriptions.status(user.id)).tracker_count == 1
    assert (await container.trackers.get(user.id, tracker.id)).scheduled


async def test_watch_api_create_patch_delete_and_targets(container):
    client = await api_client(container, 111)
    other = await api_client(container, 222)
    a = await persist(container, "sony-a")
    user = await container.users.telegram(111)
    await grant_plan(container, user.id)
    async with client, other:
        response = await client.post(
            "/api/v1/product-watches",
            json={
                "product_id": str(a.product_id),
                "market_country": "BE",
                "currency": "EUR",
                "target_price": "300",
            },
        )
        assert response.status_code == 201
        watch_id = response.json()["id"]
        duplicate = await client.post(
            "/api/v1/product-watches",
            json={"product_id": str(a.product_id), "market_country": "BE", "currency": "EUR"},
        )
        assert duplicate.json()["id"] == watch_id
        assert len((await client.get("/api/v1/product-watches")).json()) == 1
        assert (
            await other.patch(f"/api/v1/product-watches/{watch_id}", json={"enabled": False})
        ).status_code == 404
        assert (
            await client.patch(f"/api/v1/product-watches/{watch_id}", json={"enabled": None})
        ).status_code == 422
        response = await client.patch(
            f"/api/v1/product-watches/{watch_id}",
            json={"target_price": None, "notify_on_new_best": False},
        )
        assert response.status_code == 200 and response.json()["target_price"] is None
        assert (await client.delete(f"/api/v1/product-watches/{watch_id}")).status_code == 204


async def accept_price(container, offer, slug, price, availability="in_stock"):
    await make_due(container, offer.id)
    claim = next(c for c in await container.price_checks.claim_due() if c.offer_id == offer.id)
    data = (
        MockStoreProvider()
        ._offer(slug)
        .model_copy(
            update={
                "price": Decimal(price),
                "availability": availability,
            }
        )
    )
    assert await container.price_checks.accept(claim, data)
    assert not await container.price_checks.accept(claim, data)


async def event_types(container):
    async with container.sessions() as session:
        return list(
            await session.scalars(
                select(NotificationEvent.event_type).order_by(
                    NotificationEvent.created_at, NotificationEvent.id
                )
            )
        )


async def test_watch_target_drops_stock_and_flags_are_transactional(container):
    user, a, b, watch = await setup_watch(container)
    await grant_plan(container, user.id)
    await container.watches.update(user.id, watch.id, WatchPatch(target_price=Decimal(310)))
    await accept_price(container, a, "sony-a", "320")
    assert await event_types(container) == ["new_best_price"]
    await accept_price(container, a, "sony-a", "305")
    assert await event_types(container) == ["new_best_price", "target_reached"]
    await accept_price(container, a, "sony-a", "305")
    assert len(await event_types(container)) == 2
    await container.watches.update(
        user.id,
        watch.id,
        WatchPatch(
            target_price=None,
            notify_on_new_best=False,
            notify_on_price_drop=False,
        ),
    )
    await accept_price(container, a, "sony-a", "299")
    assert len(await event_types(container)) == 2
    await accept_price(container, b, "sony-b", "345", "out_of_stock")
    await accept_price(container, a, "sony-a", "299", "out_of_stock")
    assert (await container.watches.list(user.id))[0].best_offer_id is None
    await container.watches.update(user.id, watch.id, WatchPatch(notify_on_new_best=True))
    await accept_price(container, a, "sony-a", "299", "in_stock")
    assert (await event_types(container))[-1] == "best_offer_back_in_stock"


async def test_watch_drop_cooldown_and_expired_paid_notification(container):
    from pricehunter.db.models import Subscription
    from tests.integration.test_tracking import FakeSender

    user, a, b, watch = await setup_watch(container)
    container.settings.notification_cooldown_seconds = 3600
    await accept_price(container, a, "sony-a", "320")
    await accept_price(container, a, "sony-a", "315")
    assert await event_types(container) == ["new_best_price"]
    await grant_plan(container, user.id)
    await container.watches.update(user.id, watch.id, WatchPatch(target_price=Decimal(310)))
    await accept_price(container, a, "sony-a", "305")
    assert await event_types(container) == ["new_best_price", "target_reached"]
    async with container.sessions.begin() as session:
        await session.execute(
            update(Subscription).values(valid_until=utcnow() - timedelta(seconds=1))
        )
    sender = FakeSender()
    notifier = NotificationService(container.sessions, sender, container.entitlements)
    assert await notifier.send_pending() == 1
    assert sender.deliveries[0].event_type == "new_best_price"
    async with container.sessions() as session:
        target = await session.scalar(
            select(NotificationEvent).where(NotificationEvent.event_type == "target_reached")
        )
        assert target.status == "cancelled"


async def test_discovery_updates_watch_and_concurrent_replay_is_idempotent(container):
    user, a, b, watch = await setup_watch(container)
    cheaper = (
        MockStoreProvider()
        ._offer("sony-b")
        .model_copy(
            update={
                "store_slug": "new_merchant",
                "store_name": "New merchant",
                "price": Decimal(300),
            }
        )
    )
    offers = await asyncio.gather(*(container.products.persist(cheaper) for _ in range(6)))
    assert len({o.id for o in offers}) == 1
    assert await event_types(container) == ["merchant_became_cheapest"]
    assert (await container.watches.list(user.id))[0].best_offer_id == offers[0].id


async def test_watch_and_exact_creation_cannot_race_past_shared_quota(container):
    user = await container.users.telegram(111)
    a = await persist(container, "sony-a")
    b = await persist(container, "sony-b")
    await container.trackers.create(user.id, TrackerCreate(store_offer_id=a.id))
    results = await asyncio.gather(
        container.watches.create(
            user.id, WatchCreate(market_country="BE", product_id=a.product_id, currency="EUR")
        ),
        container.trackers.create(user.id, TrackerCreate(store_offer_id=b.id)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SubscriptionLimitReachedError) for r in results) == 1
    assert (await container.subscriptions.status(user.id)).tracker_count == 2


async def test_search_provider_order_partial_failures_and_persistence_errors(container):
    class FixtureProvider:
        manages_request_limits = False
        name = "fixture"

        async def search(self, *args, **kwargs):
            return [MockStoreProvider()._offer(s) for s in ("sony-b", "sony-a", "sony-white")]

    class FailedProvider:
        manages_request_limits = False
        name = "failed"

        async def search(self, *args, **kwargs):
            raise TimeoutError()

    container.registry.providers = {"fixture": FixtureProvider(), "failed": FailedProvider()}
    user = await container.users.telegram(111)
    first = await container.search.search("Sony WH1000XM6", user.id)
    container.registry.providers = dict(reversed(container.registry.providers.items()))
    second = await container.search.search("Sony WH1000XM6", user.id)
    assert first == second and first.products[0].store_count == 2
    assert first.unavailable_providers == ["failed"]
    async with container.sessions.begin() as session:
        await session.execute(update(Store).where(Store.name == "Demo Alpha").values(active=False))
    third = await container.search.search("Sony WH1000XM6", user.id)
    assert third.unavailable_providers == ["failed", "mock"]
    assert all(o.store != "Demo Alpha" for p in third.products for o in p.offers)


async def test_faster_watch_advances_existing_schedule_and_downgrade_shares_quota(container):
    from copy import copy

    from pricehunter.db.models import Subscription

    provider = copy(container.registry.get("mock"))
    provider.name = "ebay"
    container.registry.providers["ebay"] = provider
    user = await container.users.telegram(111)
    a, b = [await persist(container, slug) for slug in ("sony-a", "sony-b")]
    # A real-store listing previously scheduled for a slower Free subscriber.
    async with container.sessions.begin() as session:
        await session.execute(update(Store).values(provider_type="ebay"))
        await session.execute(
            update(StoreOffer).values(
                next_check_at=utcnow() + timedelta(hours=12),
                last_checked_at=utcnow() - timedelta(hours=3),
                refresh_sequence=1,
            )
        )
    await grant_plan(container, user.id)
    exact = await container.trackers.create(user.id, TrackerCreate(store_offer_id=a.id))
    watch = await container.watches.create(
        user.id, WatchCreate(market_country="BE", product_id=a.product_id, currency="EUR")
    )
    assert {c.offer_id for c in await container.price_checks.claim_due()} == {a.id, b.id}
    latest = await container.trackers.create(user.id, TrackerCreate(store_offer_id=b.id))
    async with container.sessions.begin() as session:
        await session.execute(
            update(Subscription).values(valid_until=utcnow() - timedelta(seconds=1))
        )
    assert (await container.trackers.get(user.id, exact.id)).scheduled
    assert (await container.watches.list(user.id))[0].id == watch.id
    assert (await container.watches.list(user.id))[0].scheduled
    assert not (await container.trackers.get(user.id, latest.id)).scheduled
    status = await container.subscriptions.status(user.id)
    assert status.tracker_count == 3 and status.scheduled_tracker_count == 2
