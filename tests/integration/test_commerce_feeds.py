from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from pricehunter.db.models import PriceObservation, Product, Store, StoreOffer
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from tests.support import market_user

pytestmark = pytest.mark.integration


async def setup_feeds(container):
    from pricehunter.providers.feeds.base import FeedSource
    from pricehunter.providers.feeds.local import FeedStoreProvider
    from pricehunter.services.feed_sync import FeedSyncService
    from pricehunter.services.merchant_programs import MerchantProgramService

    container.settings.awin_enabled = True
    provider = FeedStoreProvider("awin", container.sessions, container.settings)
    container.registry.providers = {"awin": provider}
    programs = MerchantProgramService(container.sessions, container.settings)
    sync = FeedSyncService(container.sessions, container.settings, container.products)
    return programs, sync, provider, FeedSource


def row(external_id="sony", price="329", **overrides):
    from pricehunter.domain.feeds import FeedProductData

    return FeedProductData(
        **{
            "external_id": external_id,
            "title": "Sony WH-1000XM6",
            "brand": "Sony",
            "model": "WH-1000XM6",
            "gtin": "4548736162563",
            "price": price,
            "currency": "EUR",
            "availability": "in_stock",
            "affiliate_url": "https://www.awin1.com/cread.php?test=fixture",
            **overrides,
        }
    )


async def program(service, merchant, country="BE", policy=SYNTHETIC_POLICY):
    from pricehunter.domain.feeds import MerchantProgramInput

    return await service.save(
        MerchantProgramInput(
            network="awin",
            external_merchant_id=merchant,
            market_country=country,
            display_name=f"Merchant {merchant}",
            domain="example.com",
            currency="EUR",
            external_feed_id=merchant,
            active=True,
            approved=True,
            policy=policy,
        )
    )


def source(base, rows, fail=False):
    class FixtureSource(base):
        name = "awin"

        async def stream_items(self, program) -> AsyncIterator:
            for item in rows:
                yield item
            if fail:
                raise RuntimeError("interrupted fixture")

    return FixtureSource()


async def test_one_network_two_merchants_materialize_through_existing_catalog(container):
    programs, sync, provider, base = await setup_feeds(container)
    for merchant, price in (("1", "329"), ("2", "319")):
        p = await program(programs, merchant)
        await sync.run(p.id, source(base, [row(price=price)]))
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 0
    user = await market_user(container, 740001, "BE")
    result = await container.search.search("Sony WH-1000XM6", user.id)
    assert len(result.products) == 1
    assert result.products[0].currency_groups[0].best_available_offer.price == Decimal("319")
    async with container.sessions() as session:
        assert set(await session.scalars(select(Store.name))) == {"Merchant 1", "Merchant 2"}
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 2


async def test_same_network_merchant_permissions_are_independent(container):
    from pricehunter.services.comparison_service import ComparisonReader

    programs, sync, provider, base = await setup_feeds(container)
    catalog_only = SYNTHETIC_POLICY.model_copy(
        update={
            "tracking_allowed": False,
            "price_history_allowed": False,
            "refresh_allowed": False,
        }
    )
    for merchant, price, policy in (("1", "250", catalog_only), ("2", "300", SYNTHETIC_POLICY)):
        p = await program(programs, merchant, policy=policy)
        await sync.run(p.id, source(base, [row(price=price)]))
    user = await market_user(container, 740002, "BE")
    result = await container.search.search("Sony WH-1000XM6", user.id)
    product_id = result.products[0].id
    from pricehunter.db.base import utcnow

    async with container.sessions() as session:
        summary = await ComparisonReader(container.settings, "tracking_allowed", "BE").summary(
            session, product_id, utcnow()
        )
        assert summary.groups[0].best_available_offer.price == Decimal("300")
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


async def test_interrupted_feed_does_not_publish_or_expire_current_generation(container):
    from pricehunter.db.models import MerchantFeedItem

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row(price="349")]))
    user = await market_user(container, 740003, "BE")
    await container.search.search("Sony WH-1000XM6", user.id)
    async with container.sessions() as session:
        offer = (await session.scalars(select(StoreOffer))).one()
        old_id = offer.id
    await sync.run(p.id, source(base, [row(price="329")]))
    with pytest.raises(RuntimeError):
        await sync.run(p.id, source(base, [row("other")], fail=True))
    async with container.sessions() as session:
        current = (await session.scalars(select(MerchantFeedItem))).one()
        assert current.active and current.data["price"] == "329"
        assert (await session.get(StoreOffer, old_id)).price == Decimal("329")
    await sync.run(p.id, source(base, []))
    async with container.sessions() as session:
        assert not (await session.scalars(select(MerchantFeedItem))).one().active
        assert (await session.get(StoreOffer, old_id)).availability == "in_stock"
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 2


async def test_feed_gtin_is_global_but_be_de_comparisons_are_isolated(container):
    programs, sync, provider, base = await setup_feeds(container)
    for merchant, country, price in (("1", "BE", "329"), ("2", "BE", "319"), ("3", "DE", "299")):
        p = await program(programs, merchant, country)
        await sync.run(p.id, source(base, [row(price=price)]))
    user = await market_user(container, 740004, "BE")
    be = await container.search.search("Sony WH-1000XM6", user.id, country="BE")
    de = await container.search.search("Sony WH-1000XM6", user.id, country="DE")
    assert be.products[0].id == de.products[0].id
    assert be.products[0].currency_groups[0].best_available_offer.price == Decimal("319")
    assert de.products[0].currency_groups[0].best_available_offer.price == Decimal("299")


async def test_feed_fashion_variants_remain_distinct(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    items = [
        row(
            f"shoe-{size}-{color}",
            gtin=None,
            title="Shoe runner",
            brand="Example",
            model="Runner",
            parent_external_id="runner",
            variant={"size": size, "color": color},
        )
        for size, color in (("42", "black"), ("43", "black"), ("42", "white"))
    ]
    await sync.run(p.id, source(base, items))
    user = await market_user(container, 740005, "BE")
    result = await container.search.search("Shoe runner", user.id)
    assert len(result.products) == 3
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 3


async def test_dry_run_duplicate_replay_and_conflict_leave_current_data_intact(container):
    from pricehunter.db.models import FeedSyncState, MerchantFeedItem
    from pricehunter.domain.feeds import FeedError

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row(), row("old")]))
    report = await sync.run(
        p.id, source(base, [row(price="300"), row("new"), row("new")]), dry_run=True
    )
    assert (
        report.would_insert,
        report.would_update,
        report.would_deactivate,
        report.duplicates,
    ) == (1, 1, 1, 1)
    async with container.sessions() as session:
        assert (await session.get(FeedSyncState, p.id)).generation == 1
        assert set(await session.scalars(select(MerchantFeedItem.external_id))) == {"sony", "old"}
    repeated = await sync.run(p.id, source(base, [row(), row("old"), row("old")]))
    assert (
        repeated.would_insert,
        repeated.would_update,
        repeated.would_deactivate,
        repeated.duplicates,
    ) == (0, 0, 0, 1)
    for dry in (True, False):
        with pytest.raises(FeedError, match="conflicting_duplicate"):
            await sync.run(p.id, source(base, [row(), row(price="300")]), dry_run=dry)
    async with container.sessions() as session:
        assert (await session.get(FeedSyncState, p.id)).generation == 2


async def test_feed_claims_fence_expired_workers_and_duplicate_jobs(container):
    import asyncio
    from datetime import timedelta

    from pricehunter.db.base import utcnow
    from pricehunter.db.models import FeedSyncState, MerchantFeedItem
    from pricehunter.domain.feeds import FeedError

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    claims = await asyncio.gather(sync.claim(p.id), sync.claim(p.id))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    ready, finish = asyncio.Event(), asyncio.Event()

    class Slow(base):
        name = "awin"

        async def stream_items(self, program):
            ready.set()
            await finish.wait()
            yield row(price="999")

    task = asyncio.create_task(sync.run(p.id, Slow(), token=claim[1]))
    await ready.wait()
    with pytest.raises(FeedError, match="sync_busy"):
        await sync.run(p.id, source(base, [row()]), token=claim[1])
    async with container.sessions.begin() as session:
        state = await session.get(FeedSyncState, p.id)
        state.lease_until = utcnow() - timedelta(seconds=1)
    next_claim = await sync.claim(p.id)
    await sync.run(p.id, source(base, [row(price="300")]), token=next_claim[1])
    finish.set()
    with pytest.raises(FeedError, match="lease_lost"):
        await task
    async with container.sessions() as session:
        assert (await session.scalars(select(MerchantFeedItem))).one().data["price"] == "300"
        assert (await session.get(FeedSyncState, p.id)).status == "complete"


async def test_source_version_change_and_row_limits_do_not_publish(container):
    from pricehunter.db.models import MerchantFeedItem
    from pricehunter.domain.feeds import FeedError, RejectedFeedRow

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row()]))

    class Changed(base):
        name = "awin"
        calls = 0

        async def source_version(self, program):
            self.calls += 1
            return str(self.calls)

        async def stream_items(self, program):
            yield row(price="250")

    with pytest.raises(FeedError, match="source_changed_during_sync"):
        await sync.run(p.id, Changed(), dry_run=True)
    with pytest.raises(FeedError, match="source_changed_during_sync"):
        await sync.run(p.id, Changed())
    container.settings.feed_max_rows = 1
    with pytest.raises(FeedError, match="row_limit"):
        await sync.run(p.id, source(base, [row(), row("other")]))
    container.settings.feed_max_rows = 100
    report = await sync.run(p.id, source(base, [row(), RejectedFeedRow("invalid_product")]))
    assert report.valid_rows == 1 and report.invalid_rows == 1
    async with container.sessions() as session:
        assert (await session.scalars(select(MerchantFeedItem))).one().data["price"] == "329"


async def test_versioned_staging_does_not_regress_and_old_generation_cannot_materialize(container):
    from datetime import timedelta

    from pricehunter.db.base import utcnow
    from pricehunter.db.models import MerchantFeedItem
    from pricehunter.domain.errors import ProviderPolicyError
    from pricehunter.domain.ingestion import IngestionMode

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    now = utcnow() - timedelta(days=1)
    await sync.run(p.id, source(base, [row(source_updated_at=now)]))
    old = (await provider.search("Sony", country="BE"))[0]
    await sync.run(
        p.id, source(base, [row(price="300", source_updated_at=now + timedelta(hours=1))])
    )
    await sync.run(p.id, source(base, [row(price="999", source_updated_at=now)]))
    async with container.sessions() as session:
        assert (await session.scalars(select(MerchantFeedItem))).one().data["price"] == "300"
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(old, mode=IngestionMode.SEARCH_SNAPSHOT)


async def test_program_revocation_removes_comparison_history_and_outbound_permissions(container):
    from pricehunter.db.base import utcnow
    from pricehunter.db.models import MerchantProgram
    from pricehunter.domain.errors import ProviderPolicyError
    from pricehunter.services.comparison_service import ComparisonReader

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row()]))
    user = await market_user(container, 740020, "BE")
    result = await container.search.search("Sony", user.id)
    async with container.sessions.begin() as session:
        current = await session.get(MerchantProgram, p.id)
        current.active = False
    assert await provider.search("Sony", country="BE") == []
    async with container.sessions() as session:
        offer = (await session.scalars(select(StoreOffer))).one()
        store = await session.get(Store, offer.store_id)
        with pytest.raises(ProviderPolicyError):
            container.outbound.destination(offer, store)
        for permission in (None, "tracking_allowed", "price_history_allowed"):
            summary = await ComparisonReader(container.settings, permission, "BE").summary(
                session, result.products[0].id, utcnow()
            )
            assert not summary.groups
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


async def test_shared_discovery_uses_indexed_feed_and_not_catalog_only_merchants(container):
    from pricehunter.domain.discovery import DiscoveryQuery
    from pricehunter.schemas.watches import WatchCreate

    programs, sync, provider, base = await setup_feeds(container)
    a, b = await program(programs, "1"), await program(programs, "2")
    await sync.run(a.id, source(base, [row()]))
    user = await market_user(container, 740021, "BE")
    product = (await container.search.search("Sony", user.id)).products[0]
    await container.watches.create(
        user.id, WatchCreate(product_id=product.id, market_country="BE", currency="EUR")
    )
    await sync.run(b.id, source(base, [row(price="300")]))
    assert await container.discovery.synchronize() == 1
    claims = await container.discovery.claim_due()
    assert len(claims) == 1
    assert await container.discovery.discover(claims[0])
    comparison = await container.products.product(product.id, user.id)
    assert comparison.best_available_offer.price == Decimal("300")
    assert len(await provider.discover(DiscoveryQuery("gtin", row().gtin, "BE", "EUR"))) == 2
    assert (
        len(await provider.discover(DiscoveryQuery("model", "Sony WH-1000XM6", "BE", "EUR"))) == 2
    )


@pytest.mark.parametrize("count", [1000, 100000])
async def test_large_feed_batched_storage_indexes_and_idempotent_replay(container, count):
    from sqlalchemy import event, text

    from pricehunter.db.models import MerchantFeedItem
    from pricehunter.domain.discovery import DiscoveryQuery

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    calls = 0

    def query_count(*args):
        nonlocal calls
        calls += 1

    engine = container.sessions.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", query_count)
    template = row(mpn="WHX6")

    def rows():
        for i in range(count):
            yield template.model_copy(update={"external_id": str(i), "title": f"Sony model {i}"})

    try:
        first = await sync.run(p.id, source(base, rows()))
        assert first.would_insert == count
        assert calls < count // container.settings.feed_batch_size * 6 + 40
        async with container.sessions() as session:
            first_id = await session.scalar(
                select(MerchantFeedItem.id).where(MerchantFeedItem.external_id == "0")
            )
        replay = await sync.run(p.id, source(base, rows()))
        assert (replay.would_insert, replay.would_update, replay.would_deactivate) == (0, 0, 0)
        async with container.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Product)) == 0
            assert (
                await session.scalar(
                    select(MerchantFeedItem.id).where(MerchantFeedItem.external_id == "0")
                )
                == first_id
            )
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = await session.scalar(
                text("EXPLAIN (FORMAT JSON) SELECT id FROM merchant_feed_items WHERE gtin = :gtin"),
                {"gtin": template.gtin.zfill(14)},
            )
            assert "ix_feed_gtin" in str(plan)
            mpn_plan = await session.scalar(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT id FROM merchant_feed_items "
                    "WHERE lower(data ->> 'mpn') = 'whx6'"
                )
            )
            assert "ix_feed_mpn_exact" in str(mpn_plan)
        found = await provider.discover(DiscoveryQuery("gtin", template.gtin, "BE", "EUR"))
        assert len(found) == container.settings.discovery_result_limit
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", query_count)
