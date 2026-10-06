"""Deterministic synthetic recipe, restricted to dedicated empty test databases."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from pricehunter.core.container import Container
from pricehunter.core.security import token_digest
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    APIKey,
    BillingOperation,
    FeedPendingItem,
    FxRate,
    MerchantProgram,
    NotificationEvent,
    OutboundClick,
    Store,
    StoreOffer,
    User,
)
from pricehunter.domain.billing import StarsPayment, checkout_payload
from pricehunter.domain.delivery import DeliveryContext
from pricehunter.domain.feeds import FeedProductData, MerchantProgramInput, RejectedFeedRow
from pricehunter.domain.products import Availability, ProductOfferData
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.domain.subscriptions import Plan
from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.integrity import compare, integrity, state
from pricehunter.operations.recovery import database_url, sessions_for
from pricehunter.payments.base import CheckoutContext
from pricehunter.payments.stars import TelegramStarsPaymentProvider
from pricehunter.providers.base import OfferReference
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.providers.feeds.local import FeedStoreProvider
from pricehunter.providers.mock import CATALOG, COMPARISON_CATALOG, MockStoreProvider
from pricehunter.schemas.api import TrackerCreate
from pricehunter.schemas.watches import WatchCreate


class SyntheticStars(TelegramStarsPaymentProvider):
    def __init__(self) -> None:
        super().__init__(None)

    async def create_checkout(self, context: CheckoutContext) -> str:
        return "https://example.invalid/synthetic-invoice"


class SyntheticFeed(FeedSource):
    name = "awin"

    def __init__(self, count: int = 1, version: str = "rehearsal-v2") -> None:
        self.count, self.version = count, version

    async def source_version(self, program: MerchantProgram) -> str:
        return self.version

    async def stream_items(
        self,
        program: MerchantProgram,
    ) -> AsyncIterator[FeedProductData | RejectedFeedRow]:
        for index in range(self.count):
            yield FeedProductData(
                external_id=f"synthetic-{index}",
                title="Sony WH-1000XM6 Black",
                brand="Sony",
                model="WH-1000XM6",
                gtin="4006381333931",
                variant={"color": "black"},
                price=Decimal("340"),
                currency="EUR",
                availability=Availability.IN_STOCK,
                affiliate_url="https://www.awin1.com/cread.php?synthetic=true",
                direct_url="https://example.com/synthetic-headphones",
            )


@dataclass(frozen=True, repr=False)
class Dataset:
    # Deliberately not serializable as a report; keys and identities stay in memory.
    user_ids: tuple[UUID, ...]
    api_keys: tuple[str, ...]
    product_id: UUID
    offer_id: UUID
    program_id: UUID
    payment: StarsPayment
    billing_update_id: UUID
    snapshot: ProductOfferData


def require_synthetic(container: Container) -> None:
    url = container.sessions.kw["bind"].url
    if container.settings.environment != "test" or not (url.database or "").endswith("_test"):
        raise RecoveryError("synthetic_database_required")
    if (
        any(
            not isinstance(p, (MockStoreProvider, FeedStoreProvider))
            for p in container.registry.providers.values()
        )
        or container.feed_sources
    ):
        raise RecoveryError("synthetic_providers_required")


async def seed(container: Container, users: int = 32) -> Dataset:
    require_synthetic(container)
    if not 4 <= users <= 128:
        raise RecoveryError("synthetic_user_bound")
    async with container.sessions() as session:
        if await session.scalar(select(func.count()).select_from(User)):
            raise RecoveryError("synthetic_source_not_empty")
    container.settings.stars_billing_enabled = True
    container.settings.feed_min_valid_rows = 1
    container.settings.feed_shrink_guard_min_previous_rows = 1
    container.billing.provider = SyntheticStars()
    user_ids = tuple(uuid5(NAMESPACE_URL, f"pricehunter/m5c/user/{i}") for i in range(users))
    keys = tuple(f"synthetic-m5c-api-key-{i:04d}-local-only" for i in range(users))
    async with container.sessions.begin() as session:
        session.add_all(
            User(
                id=identity,
                telegram_user_id=8_000_000 + i,
                country_code="DE",
                delivery_country="BE",
            )
            for i, identity in enumerate(user_ids)
        )
        await session.flush()
        session.add_all(
            APIKey(user_id=identity, digest=token_digest(key), label="M5C synthetic")
            for identity, key in zip(user_ids, keys, strict=True)
        )
    payments: list[StarsPayment] = []
    updates: list[UUID] = []
    for i, identity in enumerate(user_ids[1:], 1):
        plan = Plan.POWER if i == 2 else Plan.PRO
        order = await container.billing.create_checkout(
            identity, plan, title="Synthetic rehearsal", description="No external payment"
        )
        payload = checkout_payload(order.intent_id)
        await container.billing.precheckout(
            8_000_000 + i, payload, "XTR", order.product.stars, f"synthetic-query-{i}"
        )
        now = utcnow()
        payment = StarsPayment(
            8_000_000 + i,
            f"m5c-synthetic-charge-{i}",
            payload,
            "XTR",
            order.product.stars,
            now,
            now + timedelta(days=30),
            True,
            True,
        )
        update = await container.billing_intake.receive(payment)
        result = await container.billing_intake.process(update)
        if result is None or not result.applied:
            raise RecoveryError("synthetic_billing_failed")
        payments.append(payment)
        updates.append(update)
    refund = await container.billing_intake.receive(payments[-1], refund=True)
    await container.billing_intake.process(refund)
    # Pending checkout/intake and uncertain external-operation state are durable too.
    pending = await container.billing.create_checkout(
        user_ids[0], Plan.PRO, title="Synthetic pending", description="Never submitted externally"
    )
    await container.billing_intake.receive(
        StarsPayment(
            8_000_000,
            "m5c-pending-intake",
            checkout_payload(pending.intent_id),
            "XTR",
            pending.product.stars,
            utcnow(),
            utcnow() + timedelta(days=30),
            True,
            True,
        )
    )
    async with container.sessions.begin() as session:
        session.add(
            BillingOperation(
                kind="refund",
                identity="synthetic-unresolved",
                status="uncertain",
                token=uuid5(NAMESPACE_URL, "m5c/operation"),
                lease_until=utcnow() - timedelta(seconds=1),
            )
        )
    provider = container.registry.get("mock")
    snapshots = [
        await provider.resolve_url(f"https://mock.pricehunter.test/products/{slug}")
        for slug in (
            *CATALOG,
            *COMPARISON_CATALOG,
            "model-a",
            "model-b",
            "model-256",
            "delivery-alpha",
            "delivery-beta",
        )
    ]
    offers = {}
    for snapshot in snapshots:
        offers[snapshot.external_id] = await container.products.persist(snapshot)
    chosen = offers["sony-b"]
    await container.trackers.create(user_ids[1], TrackerCreate(store_offer_id=chosen.id))
    await container.watches.create(
        user_ids[1], WatchCreate(product_id=chosen.product_id, market_country="DE", currency="EUR")
    )
    claims = await container.price_checks.claim_due()
    snapshot = next(s for s in snapshots if s.external_id == "sony-b")
    for claim in claims:
        async with container.sessions() as session:
            stored = await session.get(StoreOffer, claim.offer_id)
            assert stored is not None
            store = await session.get(Store, stored.store_id)
            assert store is not None
            reference = OfferReference(
                stored.url,
                stored.external_id,
                store.slug,
                stored.refresh_sequence,
                stored.metadata_json,
            )
        current = await provider.refresh_offer(reference)
        if not await container.price_checks.accept(claim, current):
            raise RecoveryError("synthetic_refresh_failed")
        if current.external_id == "sony-b":
            snapshot = current
    await container.discovery.synchronize()
    await container.delivery.request(
        chosen.product_id, user_ids[1], DeliveryContext(country="BE"), market_country="BE"
    )
    programs = []
    for network, external in (("awin", "991"), ("cj", "992")):
        program = await container.merchant_programs.save(
            MerchantProgramInput(
                network=network,
                external_merchant_id=external,
                external_feed_id=external,
                market_country="BE",
                currency="EUR",
                domain="example.com",
                display_name="Synthetic Recovery Retailer",
            )
        )
        program = await container.merchant_programs.review(
            program.id,
            SYNTHETIC_POLICY,
            expected_version=program.version,
            reason="Synthetic rights only",
        )
        programs.append(program)
    p = programs[0]
    container.settings.awin_enabled = True
    container.settings.feed_program_ids = [str(p.id)]
    await container.feed_validation.run(p.id, SyntheticFeed())
    p = await container.merchant_programs.activate(
        p.id, expected_version=p.version, reason="Synthetic fixture only"
    )
    await container.feed_sync.run(p.id, SyntheticFeed(10, "rehearsal-v1"))
    await container.feed_sync.run(
        p.id, SyntheticFeed(), allow_shrink=True, confirm=True, reason="Synthetic shrink rehearsal"
    )
    feed_provider = FeedStoreProvider("awin", container.sessions, container.settings)
    container.registry.providers["awin"] = feed_provider
    materialized = await feed_provider.search("Sony", country="BE")
    for row in materialized:
        await container.products.persist(row)
    async with container.sessions() as session:
        a, b = [await session.get(Store, program.store_id) for program in programs]
        assert a is not None and b is not None
    await container.merchants.reassign(
        b.id,
        a.merchant_id,
        expected_source_merchant_id=b.merchant_id,
        expected_source_version=b.merchant.version,
        expected_version=a.merchant.version,
        reason="Synthetic cross-network identity",
        confirm=True,
    )
    async with container.sessions.begin() as session:
        event = await session.scalar(
            select(NotificationEvent)
            .where(NotificationEvent.product_watch_id.is_not(None))
            .limit(1)
        )
        if event is None:
            raise RecoveryError("synthetic_outbox_missing")
        for status in ("sent", "uncertain"):
            session.add(
                NotificationEvent(
                    product_watch_id=event.product_watch_id,
                    snapshot=event.snapshot,
                    event_type=event.event_type,
                    price=event.price,
                    currency=event.currency,
                    deduplication_key=f"m5c-synthetic-{status}",
                    status=status,
                    sent_at=utcnow() if status == "sent" else None,
                    attempt_started_at=utcnow() - timedelta(minutes=3),
                )
            )
        session.add(
            FxRate(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.1"),
                effective_date=utcnow().date(),
                source="synthetic",
            )
        )
        stored_offer = await session.get(StoreOffer, chosen.id)
        assert stored_offer is not None
        store = await session.get(Store, stored_offer.store_id)
        assert store is not None
        session.add(
            OutboundClick(
                offer_id=chosen.id,
                store_id=store.id,
                merchant_id=store.merchant_id,
                surface="api",
                market_country="DE",
                opaque_click_reference="m5c-synthetic-click",
            )
        )
        session.add(
            FeedPendingItem(
                merchant_program_id=p.id,
                attempt=uuid5(NAMESPACE_URL, "m5c/interrupted-feed"),
                external_id="interrupted",
                data={"synthetic": True},
                fingerprint="0" * 64,
                normalized_title="interrupted",
            )
        )
    await integrity(container.sessions)
    return Dataset(
        user_ids, keys, chosen.product_id, chosen.id, p.id, payments[0], updates[0], snapshot
    )


@asynccontextmanager
async def restored_container(container: Container, url: str) -> AsyncIterator[Container]:
    sessions = sessions_for(database_url(url))
    settings = container.settings.model_copy(deep=True)
    settings.awin_enabled = False
    restored = Container(settings, sessions=sessions, redis=container.redis)
    settings.awin_enabled = True
    restored.registry.providers["awin"] = FeedStoreProvider("awin", sessions, restored.settings)
    try:
        yield restored
    finally:
        await restored.close()
        await sessions.kw["bind"].dispose()


async def replay(container: Container, dataset: Dataset) -> None:
    require_synthetic(container)
    before = await state(container.sessions)
    identity = await container.billing_intake.receive(dataset.payment)
    if identity != dataset.billing_update_id or await container.billing_intake.process(identity):
        raise RecoveryError("billing_replay_not_idempotent")
    result = await container.billing.process_successful_payment(dataset.payment)
    if result.applied:
        raise RecoveryError("billing_replay_not_idempotent")
    offer = await container.products.persist(dataset.snapshot)
    if offer.id != dataset.offer_id:
        raise RecoveryError("catalog_replay_not_idempotent")
    async with container.sessions.begin() as session:
        await container.watches.evaluate(session, dataset.product_id, market_country="DE")
    await container.feed_sync.run(dataset.program_id, SyntheticFeed())
    for row in await container.registry.get("awin").search("Sony", country="BE"):
        await container.products.persist(row)
    after = await state(container.sessions)
    # Mutable freshness/scheduling fields may advance; financial and history payloads cannot.
    for name in (
        "payment_events",
        "subscription_periods",
        "notification_events",
        "price_observations",
        "best_price_events",
        "merchant_audits",
        "merchant_program_audits",
        "feed_publication_audits",
    ):
        compare({name: after[name]}, {name: before[name]})
    for name in before:
        if (after[name].rows, after[name].primary_keys_sha256) != (
            before[name].rows,
            before[name].primary_keys_sha256,
        ):
            raise RecoveryError("replay_identity_changed")
    await integrity(container.sessions)


async def redis_loss(container: Container) -> None:
    require_synthetic(container)
    pool = container.redis.connection_pool
    if not pool.connection_class.__module__.startswith("fakeredis") and (
        pool.connection_kwargs.get("host") not in ("localhost", "127.0.0.1")
        or pool.connection_kwargs.get("db") != 14
    ):
        raise RecoveryError("synthetic_redis_required")
    before = await state(container.sessions)
    await container.redis.set("ph:m5c:conversation", "synthetic", ex=60)
    await container.limiter.user(uuid5(NAMESPACE_URL, "pricehunter/m5c/user/0"))
    await container.redis.flushdb()
    await container.runtime.run()
    if await container.redis.get("ph:m5c:conversation") is not None:
        raise RecoveryError("redis_reset_failed")
    compare(await state(container.sessions), before)
    await integrity(container.sessions)
