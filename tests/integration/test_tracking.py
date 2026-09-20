import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    NotificationEvent,
    PaymentEvent,
    PriceObservation,
    Product,
    StoreOffer,
)
from pricehunter.domain.errors import ProductNotFoundError, SubscriptionLimitReachedError
from pricehunter.payments.base import VerifiedPayment
from pricehunter.schemas.api import TrackerCreate, TrackerPatch
from pricehunter.services.notification_service import NotificationService, RetryDelivery
from pricehunter.services.payment_service import PaymentService
from tests.support import grant_plan

pytestmark = pytest.mark.integration
URL = "https://mock.pricehunter.test/products/headphones"


class FakeSender:
    def __init__(self):
        self.deliveries = []

    async def send(self, delivery):
        self.deliveries.append(delivery)
        return len(self.deliveries)


async def setup_tracker(container, telegram_id=123):
    user = await container.users.telegram(telegram_id)
    offer = await container.products.resolve(URL, user.id)
    tracker = await container.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
    return user, offer, tracker


async def test_complete_tracking_flow_and_restart(container):
    user, offer, tracker = await setup_tracker(container)
    assert offer.price == Decimal(100)
    again = await container.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
    assert tracker.id == again.id
    await grant_plan(container, user.id)
    await container.trackers.update(user.id, tracker.id, TrackerPatch(target_price=Decimal(96)))
    claims = await container.price_checks.claim_due()
    assert len(claims) == 1
    assert await container.price_checks.refresh(claims[0])
    assert not await container.price_checks.refresh(claims[0])
    sender = FakeSender()
    notifier = NotificationService(container.sessions, sender, container.entitlements)
    assert await notifier.send_pending() == 1
    assert await notifier.send_pending() == 0
    assert sender.deliveries[0].event_type == "target_reached"
    assert sender.deliveries[0].price == Decimal(95)
    # Fresh application services read committed state, including mock sequence/history.
    from pricehunter.services.product_service import ProductService

    fresh = ProductService(
        container.sessions,
        container.registry,
        container.limiter,
        container.settings,
        container.entitlements,
    )
    assert (await fresh.offer(offer.id)).price == Decimal(95)
    assert (await fresh.resolve(URL, user.id)).price == Decimal(95)
    history = await fresh.history(offer.id, user.id)
    assert history.count == 2
    assert history.minimum == 95 and history.maximum == 100 and history.average == Decimal("97.5")
    assert history.change_since_tracking == -5
    items = await container.trackers.list(user.id)
    assert len(items) == 1 and items[0].target_price == 96
    await container.trackers.delete(user.id, tracker.id)
    await container.trackers.delete(user.id, tracker.id)
    assert await container.trackers.list(user.id) == []


@pytest.mark.parametrize("subscriber_count", [2, 100])
async def test_many_trackers_one_refresh_concurrent_claims(
    container, monkeypatch, subscriber_count
):
    user, offer, _ = await setup_tracker(container)
    for index in range(subscriber_count - 1):
        other = await container.users.telegram(1000 + index)
        await container.trackers.create(other.id, TrackerCreate(store_offer_id=offer.id))
    one, two = await asyncio.gather(
        container.price_checks.claim_due(), container.price_checks.claim_due()
    )
    claims = one + two
    assert len(claims) == 1
    provider = container.registry.get("mock")
    original = provider.refresh_offer
    calls = 0

    async def refresh(reference):
        nonlocal calls
        calls += 1
        assert container.sessions.kw["bind"].pool.checkedout() == 0
        return await original(reference)

    monkeypatch.setattr(provider, "refresh_offer", refresh)
    accepted = await asyncio.gather(*(container.price_checks.refresh(claims[0]) for _ in range(2)))
    assert sum(accepted) == 1
    assert calls == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(NotificationEvent))
            == subscriber_count
        )


async def test_repeated_target_crossings_obey_cooldown(container):
    container.settings.notification_cooldown_seconds = 3600
    user, offer, tracker = await setup_tracker(container)
    await grant_plan(container, user.id)
    await container.trackers.update(user.id, tracker.id, TrackerPatch(target_price=Decimal(96)))
    data = await container.registry.get("mock").resolve_url(URL)
    for price in ("95", "100", "90"):
        await make_due(container, offer.id)
        claim = (await container.price_checks.claim_due())[0]
        assert await container.price_checks.accept(
            claim, data.model_copy(update={"price": Decimal(price)})
        )
    async with container.sessions() as session:
        events = list(await session.scalars(select(NotificationEvent)))
        assert len(events) == 1 and events[0].event_type == "target_reached"


async def test_crashed_delivery_is_recovered_as_uncertain(container):
    await setup_tracker(container)
    await container.price_checks.refresh((await container.price_checks.claim_due())[0])
    service = NotificationService(container.sessions, FakeSender(), container.entitlements)
    assert await service.claim() is not None
    async with container.sessions.begin() as session:
        await session.execute(
            update(NotificationEvent).values(
                attempt_started_at=utcnow() - timedelta(minutes=3),
            )
        )
    assert await service.send_pending() == 0
    async with container.sessions() as session:
        assert (await session.scalars(select(NotificationEvent))).one().status == "uncertain"


async def test_concurrent_resolve_and_quota(container):
    user = await container.users.telegram(123)
    offers = await asyncio.gather(*(container.products.resolve(URL, user.id) for _ in range(5)))
    assert len({offer.id for offer in offers}) == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1
    slugs = ["headphones", "coffee-machine", "sneakers-42", "sneakers-44", "camera", "keyboard"]
    offers = [
        await container.products.resolve(f"https://mock.pricehunter.test/products/{slug}", user.id)
        for slug in slugs
    ]
    results = await asyncio.gather(
        *(container.trackers.create(user.id, TrackerCreate(store_offer_id=o.id)) for o in offers),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SubscriptionLimitReachedError) for r in results) == 4
    assert len(await container.trackers.list(user.id)) == 2


async def test_ownership_pause_resume_and_cancel_pending(container):
    user, offer, tracker = await setup_tracker(container)
    other = await container.users.telegram(456)
    with pytest.raises(ProductNotFoundError):
        await container.trackers.update(other.id, tracker.id, TrackerPatch(enabled=False))
    await container.trackers.delete(other.id, tracker.id)
    await container.trackers.update(user.id, tracker.id, TrackerPatch(enabled=False))
    assert await container.price_checks.claim_due() == []
    await container.trackers.update(user.id, tracker.id, TrackerPatch(enabled=True))
    claim = (await container.price_checks.claim_due())[0]
    await container.price_checks.refresh(claim)
    await container.trackers.update(user.id, tracker.id, TrackerPatch(enabled=False))
    assert (
        await NotificationService(
            container.sessions, FakeSender(), container.entitlements
        ).send_pending()
        == 0
    )


async def make_due(container, offer_id):
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == offer_id)
            .values(
                next_check_at=utcnow() - timedelta(seconds=1),
                last_checked_at=utcnow() - timedelta(days=1),
            )
        )


async def test_anomaly_requires_confirmation_currency_never_changes(container):
    user, offer, tracker = await setup_tracker(container)
    data = await container.registry.get("mock").resolve_url(URL)
    suspicious = data.model_copy(update={"price": Decimal("1.00")})
    claim = (await container.price_checks.claim_due())[0]
    assert not await container.price_checks.accept(claim, suspicious)
    assert (await container.products.offer(offer.id)).price == 100
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0
    await make_due(container, offer.id)
    claim = (await container.price_checks.claim_due())[0]
    assert await container.price_checks.accept(claim, suspicious)
    assert (await container.products.offer(offer.id)).price == 1
    for _ in range(2):
        await make_due(container, offer.id)
        claim = (await container.price_checks.claim_due())[0]
        assert not await container.price_checks.accept(
            claim, suspicious.model_copy(update={"currency": "USD"})
        )
    assert (await container.products.offer(offer.id)).currency == "EUR"


async def test_expired_lease_fences_stale_worker_and_retries(container):
    _, offer, _ = await setup_tracker(container)
    old = (await container.price_checks.claim_due())[0]
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == offer.id)
            .values(
                lease_until=utcnow() - timedelta(seconds=1),
            )
        )
    new = (await container.price_checks.claim_due())[0]
    assert old.token != new.token
    assert not await container.price_checks.refresh(old)
    await container.price_checks.failed(new)
    assert await container.price_checks.claim_due() == []
    async with container.sessions() as session:
        stored = await session.get(StoreOffer, offer.id)
        assert stored.failure_count == 1 and stored.lease_token is None


async def test_notification_ambiguous_failure_is_not_resent(container):
    await setup_tracker(container)
    await container.price_checks.refresh((await container.price_checks.claim_due())[0])

    class TimeoutSender:
        async def send(self, delivery):
            raise TimeoutError("Outcome is unknown")

    service = NotificationService(container.sessions, TimeoutSender(), container.entitlements)
    assert await service.send_pending() == 0
    assert (
        await NotificationService(
            container.sessions, FakeSender(), container.entitlements
        ).send_pending()
        == 0
    )
    async with container.sessions() as session:
        assert (await session.scalars(select(NotificationEvent))).one().status == "uncertain"


async def test_known_telegram_retry_and_delivery_claim_concurrency(container):
    await setup_tracker(container)
    await container.price_checks.refresh((await container.price_checks.claim_due())[0])

    class FloodSender:
        async def send(self, delivery):
            raise RetryDelivery(30)

    assert (
        await NotificationService(
            container.sessions, FloodSender(), container.entitlements
        ).send_pending()
        == 0
    )
    async with container.sessions.begin() as session:
        event = (await session.scalars(select(NotificationEvent))).one()
        assert event.status == "pending" and event.attempts == 1
        event.available_at = utcnow() - timedelta(seconds=1)
    sender = FakeSender()
    service = NotificationService(container.sessions, sender, container.entitlements)
    results = await asyncio.gather(service.send_pending(), service.send_pending())
    assert sum(results) == 1 and len(sender.deliveries) == 1


async def test_payment_idempotency(container):
    user = await container.users.telegram(123)
    payment = VerifiedPayment(
        "telegram_stars",
        "unique-charge",
        user.id,
        "successful_payment",
        Decimal(250),
        "XTR",
        "paid",
    )
    service = PaymentService(container.sessions)
    results = await asyncio.gather(*(service.record_verified(payment) for _ in range(5)))
    assert sum(results) == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentEvent)) == 1
