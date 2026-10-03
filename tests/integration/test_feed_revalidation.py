from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from sqlalchemy import func, select

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    FeedSyncState,
    MerchantFeedItem,
    NotificationEvent,
    PriceObservation,
    StoreOffer,
)
from pricehunter.domain.errors import ProviderPolicyError
from pricehunter.domain.ingestion import IngestionMode
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.schemas.watches import WatchCreate
from tests.integration.test_commerce_feeds import program, row, setup_feeds, source
from tests.integration.test_market_snapshots import SnapshotProvider, market_offer
from tests.support import market_user

pytestmark = pytest.mark.integration


@pytest.fixture
def feed_clock(monkeypatch):
    class Clock:
        now = utcnow()

        def advance(self, seconds):
            self.now += timedelta(seconds=seconds)

    clock = Clock()
    for name in (
        "services.feed_sync",
        "services.feed_materialization",
        "services.policy_resolver",
        "services.snapshot_ingestion",
        "providers.feeds.local",
    ):
        monkeypatch.setattr(import_module(f"pricehunter.{name}"), "utcnow", lambda: clock.now)
    return clock


@pytest.mark.parametrize("version_delta", [-3600, 0, 3600])
async def test_completed_generation_revalidates_presence_without_regressing_content(
    container, feed_clock, version_delta
):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    timestamp = feed_clock.now - timedelta(days=1)
    await sync.run(p.id, source(base, [row(source_updated_at=timestamp, mpn="original")]))
    async with container.sessions() as session:
        first = (await session.scalars(select(MerchantFeedItem))).one()
    feed_clock.advance(30)
    report = await sync.run(
        p.id,
        source(
            base,
            [
                row(
                    price="199",
                    title="Changed title",
                    mpn="changed",
                    source_updated_at=timestamp + timedelta(seconds=version_delta),
                )
            ],
        ),
    )
    async with container.sessions() as session:
        current = (await session.scalars(select(MerchantFeedItem))).one()
        assert current.id == first.id and current.active
        assert current.seen_at == report.completed_at == feed_clock.now
        assert current.seen_at > first.seen_at and current.feed_generation == 2
        if version_delta <= 0:
            assert (
                current.data,
                current.fingerprint,
                current.brand_mpn,
                current.normalized_title,
            ) == (
                first.data,
                first.fingerprint,
                first.brand_mpn,
                first.normalized_title,
            )
        else:
            assert current.data["price"] == "199"
            assert current.fingerprint != first.fingerprint and current.brand_mpn != first.brand_mpn


@pytest.mark.parametrize("history", [False, True])
async def test_unchanged_source_version_requires_full_download_when_presence_expires(
    container, feed_clock, history
):
    programs, sync, provider, base = await setup_feeds(container)
    policy = SYNTHETIC_POLICY.model_copy(
        update={
            "max_cache_seconds": 60,
            "tracking_allowed": history,
            "price_history_allowed": history,
        }
    )
    p = await program(programs, "1", policy=policy)
    initial = feed_clock.now
    data = row(source_updated_at=initial - timedelta(days=1))

    class Versioned(base):
        name = "awin"
        downloads = 0

        async def source_version(self, program):
            return "same-version"

        async def stream_items(self, program):
            self.downloads += 1
            yield data

    feed = Versioned()
    await sync.run(p.id, feed)
    for cycle in range(1, 4):
        feed_clock.advance(30)
        await sync.run(p.id, feed)
        assert feed.downloads == cycle
        async with container.sessions() as session:
            state = await session.get(FeedSyncState, p.id)
            assert state.last_success_at == initial + timedelta(seconds=60 * (cycle - 1))
            assert state.next_sync_at <= initial + timedelta(seconds=60 * cycle)
        feed_clock.advance(30)
        claim = await sync.claim()
        assert claim is not None and claim[0] == p.id
        await sync.run(p.id, feed, token=claim[1])
        assert feed.downloads == cycle + 1
        assert await provider.search("Sony", country="BE")
        async with container.sessions() as session:
            item = (await session.scalars(select(MerchantFeedItem))).one()
            assert item.seen_at == feed_clock.now and item.feed_generation == cycle + 1


async def test_active_cache_eviction_reschedules_reacquisition_immediately(container, feed_clock):
    programs, sync, provider, base = await setup_feeds(container)
    policy = SYNTHETIC_POLICY.model_copy(
        update={
            "tracking_allowed": False,
            "price_history_allowed": False,
            "max_cache_seconds": 60,
        }
    )
    p = await program(programs, "1", policy=policy)
    await sync.run(p.id, source(base, [row()]))
    # Include already scheduled programs from pre-fix deployments / changed cache policy.
    async with container.sessions.begin() as session:
        state = await session.get(FeedSyncState, p.id)
        state.next_sync_at = feed_clock.now + timedelta(hours=6)
    feed_clock.advance(61)
    assert await sync.retain() == 1
    assert await provider.search("Sony", country="BE") == []
    claim = await sync.claim()
    assert claim is not None and claim[0] == p.id
    await sync.run(p.id, source(base, [row()]), token=claim[1])
    assert await provider.search("Sony", country="BE")


async def test_identical_listing_reappears_without_new_observation(container, feed_clock):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    data = row(source_updated_at=feed_clock.now - timedelta(days=1))
    await sync.run(p.id, source(base, [data]))
    first = await container.products.persist(
        (await provider.search("Sony", country="BE"))[0], mode=IngestionMode.SEARCH_SNAPSHOT
    )
    feed_clock.advance(10)
    await sync.run(p.id, source(base, []))
    async with container.sessions() as session:
        assert not (await session.get(StoreOffer, first.id)).catalog_active
    feed_clock.advance(10)
    await sync.run(p.id, source(base, [data]))
    async with container.sessions() as session:
        current = await session.get(StoreOffer, first.id)
        assert current.catalog_active and current.feed_generation == 3
        assert current.last_checked_at == feed_clock.now
        assert current.price == 329 and current.source_updated_at == data.source_updated_at
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0


async def test_feed_revalidation_renews_only_confirmed_time_without_price_side_effects(
    container, feed_clock
):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    data = row(source_updated_at=feed_clock.now - timedelta(days=1))
    await sync.run(p.id, source(base, [data]))
    old_candidate = (await provider.search("Sony", country="BE"))[0]
    first = await container.products.persist(old_candidate, mode=IngestionMode.SEARCH_SNAPSHOT)
    user = await market_user(container, 742001, "BE")
    await container.watches.create(
        user.id,
        WatchCreate(
            product_id=first.product_id,
            currency="EUR",
            market_country="BE",
        ),
    )
    async with container.sessions() as session:
        event_count = await session.scalar(select(func.count()).select_from(BestPriceEvent))
    feed_clock.advance(20)
    await sync.run(p.id, source(base, [data]))
    confirmed_at = feed_clock.now
    candidate = (await provider.search("Sony", country="BE"))[0]
    feed_clock.advance(20)
    await container.products.persist(candidate, mode=IngestionMode.SEARCH_SNAPSHOT)
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(old_candidate, mode=IngestionMode.SEARCH_SNAPSHOT)
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(
            candidate.model_copy(update={"price": Decimal("1")}),
            mode=IngestionMode.SEARCH_SNAPSHOT,
        )
    async with container.sessions() as session:
        current = await session.get(StoreOffer, first.id)
        assert current.last_checked_at == confirmed_at and current.catalog_active
        assert current.price == 329 and current.feed_generation == 2
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1
        assert await session.scalar(select(func.count()).select_from(BestPriceEvent)) == event_count
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0


@pytest.mark.parametrize("version_delta", [-3600, 0])
async def test_generic_stale_snapshot_cannot_reactivate_or_renew(
    container, feed_clock, version_delta
):
    container.settings.provider_data_policies["rakuten"] = SYNTHETIC_POLICY
    data = market_offer("BE", "349").model_copy(
        update={
            "provider": "rakuten",
            "source_updated_at": feed_clock.now - timedelta(days=1),
        }
    )
    container.registry.providers["rakuten"] = SnapshotProvider(data)
    first = await container.products.persist(data, mode=IngestionMode.SEARCH_SNAPSHOT)
    async with container.sessions.begin() as session:
        stored = await session.get(StoreOffer, first.id)
        stored.catalog_active = False
        checked = stored.last_checked_at
    feed_clock.advance(30)
    await container.products.persist(
        data.model_copy(
            update={
                "price": Decimal("1"),
                "source_updated_at": data.source_updated_at + timedelta(seconds=version_delta),
            }
        ),
        mode=IngestionMode.SEARCH_SNAPSHOT,
    )
    async with container.sessions() as session:
        stored = await session.get(StoreOffer, first.id)
        assert (
            stored.price == 349 and not stored.catalog_active and stored.last_checked_at == checked
        )
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


@pytest.mark.parametrize("missing_evidence", ["generation", "completion", "future_presence"])
async def test_revalidation_requires_completed_current_generation(
    container, feed_clock, missing_evidence
):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row(source_updated_at=feed_clock.now - timedelta(days=1))]))
    candidate = (await provider.search("Sony", country="BE"))[0]
    offer = await container.products.persist(candidate, mode=IngestionMode.SEARCH_SNAPSHOT)
    async with container.sessions.begin() as session:
        state = await session.get(FeedSyncState, p.id)
        if missing_evidence == "generation":
            state.generation += 1
        elif missing_evidence == "completion":
            state.last_success_at = None
        else:
            item = (await session.scalars(select(MerchantFeedItem))).one()
            item.seen_at = state.last_success_at = feed_clock.now + timedelta(hours=1)
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(candidate, mode=IngestionMode.SEARCH_SNAPSHOT)
    async with container.sessions() as session:
        assert (await session.get(StoreOffer, offer.id)).last_checked_at == feed_clock.now
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


async def test_partial_generation_cannot_revalidate_presence(container, feed_clock):
    programs, sync, provider, base = await setup_feeds(container)
    container.settings.feed_batch_size = 1
    p = await program(programs, "1")
    data = row(source_updated_at=feed_clock.now - timedelta(days=1))
    confirmed = feed_clock.now
    await sync.run(p.id, source(base, [data]))
    offer = await container.products.persist(
        (await provider.search("Sony", country="BE"))[0],
        mode=IngestionMode.SEARCH_SNAPSHOT,
    )
    feed_clock.advance(30)
    with pytest.raises(RuntimeError, match="interrupted fixture"):
        await sync.run(p.id, source(base, [data], fail=True))
    async with container.sessions() as session:
        item = (await session.scalars(select(MerchantFeedItem))).one()
        current = await session.get(StoreOffer, offer.id)
        assert item.active and item.seen_at == current.last_checked_at == confirmed
        assert item.feed_generation == current.feed_generation == 1
        assert (await session.get(FeedSyncState, p.id)).generation == 1
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


async def test_retention_waits_for_active_sync_and_leaves_inactive_cleanup_schedule(
    container, feed_clock
):
    programs, sync, provider, base = await setup_feeds(container)
    policy = SYNTHETIC_POLICY.model_copy(
        update={
            "tracking_allowed": False,
            "price_history_allowed": False,
            "max_cache_seconds": 60,
        }
    )
    p = await program(programs, "1", policy=policy)
    await sync.run(p.id, source(base, [row()]))
    feed_clock.advance(61)
    claim = await sync.claim(p.id)
    assert claim is not None
    assert await sync.retain() == 0  # Acquisition already owns this program.
    await sync.run(p.id, source(base, []), token=claim[1])
    async with container.sessions.begin() as session:
        item = (await session.scalars(select(MerchantFeedItem))).one()
        item.seen_at = feed_clock.now - timedelta(days=10)
        state = await session.get(FeedSyncState, p.id)
        scheduled = state.next_sync_at = feed_clock.now + timedelta(hours=4)
    assert await sync.retain() == 1
    async with container.sessions() as session:
        assert (await session.get(FeedSyncState, p.id)).next_sync_at == scheduled
