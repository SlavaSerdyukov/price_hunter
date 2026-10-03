from argparse import Namespace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from pricehunter.apps import feed_admin
from pricehunter.core.container import Container
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    FeedPendingItem,
    FeedSyncState,
    MerchantFeedItem,
    PriceObservation,
    Store,
    StoreOffer,
)
from pricehunter.domain.errors import ProviderPolicyError
from pricehunter.domain.feeds import FeedError
from pricehunter.domain.ingestion import IngestionMode
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.jobs.worker import schedule_feeds, sync_feed
from tests.integration.test_commerce_feeds import program, row, setup_feeds, source
from tests.support import market_user

pytestmark = pytest.mark.integration


async def test_same_merchant_country_domains_and_mpn_search(container):
    programs, sync, provider, base = await setup_feeds(container)
    be = await program(programs, "1", "BE")
    de = await program(
        programs, "1", "DE", policy=SYNTHETIC_POLICY.model_copy(update={"affiliate_allowed": False})
    )
    async with container.sessions.begin() as session:
        merchant = await session.get(type(de), de.id)
        merchant.domain = "example.de"
    await sync.run(be.id, source(base, [row(mpn="WHX6")]))
    await sync.run(
        de.id,
        source(base, [row(mpn="WHX6", direct_url="https://example.de/sony")]),
    )
    candidates = await provider.search("WHX6", country="DE")
    assert len(candidates) == 1 and candidates[0].merchant_program_id == de.id
    first = await container.products.persist(candidates[0], mode=IngestionMode.SEARCH_SNAPSHOT)
    repeated = await container.products.persist(candidates[0], mode=IngestionMode.SEARCH_SNAPSHOT)
    assert first.id == repeated.id and repeated.url == "https://example.de/sony"
    assert be.store_id == de.store_id
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Store)) == 1


async def test_expired_staging_and_revoked_refresh_cannot_update_a_listing(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row()]))
    data = (await provider.search("Sony", country="BE"))[0]
    offer = await container.products.persist(data, mode=IngestionMode.SEARCH_SNAPSHOT)
    async with container.sessions.begin() as session:
        merchant = await session.get(type(p), p.id)
        merchant.policy_data = {**merchant.policy_data, "refresh_allowed": False}
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(
            data, mode=IngestionMode.SEARCH_SNAPSHOT, require_refresh_permission=True
        )
    async with container.sessions.begin() as session:
        merchant = await session.get(type(p), p.id)
        merchant.policy_data = {**merchant.policy_data, "refresh_allowed": True}
        item = (await session.scalars(select(MerchantFeedItem))).one()
        item.seen_at = utcnow() - timedelta(days=366)
        stored = await session.get(StoreOffer, offer.id)
        stored.feed_generation = 0
    assert await provider.search("Sony", country="BE") == []
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(data, mode=IngestionMode.SEARCH_SNAPSHOT)
    from pricehunter.services.feed_materialization import FeedMaterializationService

    assert await FeedMaterializationService(container.products).refresh(p.id) == 1
    async with container.sessions() as session:
        stored = await session.get(StoreOffer, offer.id)
        assert not stored.catalog_active and stored.price == 329
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


async def test_feed_worker_scheduling_lost_enqueue_and_recovery(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    container.settings.feed_program_ids = [str(p.id)]
    container.feed_sources = {"awin": source(base, [row()])}
    calls = []

    class Queue:
        broken = True

        async def enqueue_job(self, *args, **kwargs):
            if self.broken:
                raise RuntimeError("queue unavailable")
            calls.append((args, kwargs))

    queue = Queue()
    ctx = {"container": container, "redis": queue}
    assert await schedule_feeds(ctx) == 0
    async with container.sessions.begin() as session:
        state = await session.get(FeedSyncState, p.id)
        assert state.lease_token is not None
        state.lease_until = utcnow() - timedelta(seconds=1)
    queue.broken = False
    assert await schedule_feeds(ctx) == 1
    assert len(calls) == 1 and calls[0][0][0] == "sync_feed"
    assert await sync_feed(ctx, *calls[0][0][1:])
    assert await schedule_feeds(ctx) == 0
    container.feed_sources = {}
    assert await schedule_feeds(ctx) == 0
    assert not await sync_feed(ctx, str(p.id), str(uuid4()))


async def test_unchanged_feed_version_avoids_download_but_missing_cache_reacquires(container):
    programs, sync, provider, base = await setup_feeds(container)
    catalog_only = SYNTHETIC_POLICY.model_copy(
        update={"tracking_allowed": False, "price_history_allowed": False, "max_cache_seconds": 60}
    )
    p = await program(programs, "1", policy=catalog_only)

    class Versioned(base):
        name = "awin"
        downloads = 0

        async def source_version(self, program):
            return "v1"

        async def stream_items(self, program):
            self.downloads += 1
            yield row()

    feed = Versioned()
    await sync.run(p.id, feed)
    await sync.run(p.id, feed)
    assert feed.downloads == 1
    async with container.sessions.begin() as session:
        item = (await session.scalars(select(MerchantFeedItem))).one()
        item.seen_at = utcnow() - timedelta(hours=1)
    assert await sync.retain() == 1
    await sync.run(p.id, feed)
    assert feed.downloads == 2


async def test_retention_never_removes_canonical_history(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row()]))
    user = await market_user(container, 741000, "BE")
    await container.search.search("Sony", user.id)
    await sync.run(p.id, source(base, []))
    async with container.sessions.begin() as session:
        item = (await session.scalars(select(MerchantFeedItem))).one()
        item.seen_at = utcnow() - timedelta(days=10)
        session.add(
            FeedPendingItem(
                merchant_program_id=p.id,
                attempt=uuid4(),
                external_id="old",
                data={},
                fingerprint="x" * 64,
                normalized_title="old",
                created_at=utcnow() - timedelta(days=10),
            )
        )
    assert await sync.retain() == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(FeedPendingItem)) == 0
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1


async def test_materialized_identity_conflict_is_isolated_and_recoverable(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    await sync.run(p.id, source(base, [row(), row("second")]))
    user = await market_user(container, 741001, "BE")
    await container.search.search("Sony", user.id)
    await sync.run(p.id, source(base, [row(model="Different"), row("second", price="299")]))
    async with container.sessions() as session:
        items = {o.external_id: o for o in await session.scalars(select(StoreOffer))}
        assert not items["sony"].catalog_active
        assert items["second"].price == 299
    await sync.run(p.id, source(base, [row(price="300"), row("second", price="299")]))
    async with container.sessions() as session:
        item = await session.scalar(select(StoreOffer).where(StoreOffer.external_id == "sony"))
        assert item.catalog_active and item.price == 300


async def test_commission_is_not_a_ranking_input_and_catalog_only_refresh_needs_search(container):
    programs, sync, provider, base = await setup_feeds(container)
    policy = SYNTHETIC_POLICY.model_copy(
        update={"tracking_allowed": False, "price_history_allowed": False, "refresh_allowed": False}
    )
    p1, p2 = (
        await program(programs, "1", policy=policy),
        await program(programs, "2", policy=policy),
    )
    await sync.run(p1.id, source(base, [row(price="300", metadata={"commission": 2})]))
    await sync.run(p2.id, source(base, [row(price="300", metadata={"commission": 15})]))
    user = await market_user(container, 741002, "BE")
    before = (await container.search.search("Sony", user.id)).products[0]
    assert before.best_available_offer.store_slug == "awin-1"
    await sync.run(p1.id, source(base, [row(price="299", metadata={"commission": 1})]))
    async with container.sessions() as session:
        assert set(await session.scalars(select(StoreOffer.price))) == {300}
    after = (await container.search.search("Sony", user.id)).products[0]
    assert after.best_available_offer.price == 299
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 0


async def test_feed_admin_reports_and_dry_run_do_not_expose_urls(container, capsys, tmp_path):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    container.feed_sources = {"awin": source(base, [row()])}
    await sync.run(p.id, container.feed_sources["awin"])
    for command in (
        "merchant-programs",
        "feed-status",
        "feed-diagnostics",
        "feed-search",
        "merchant-program-check",
        "feed-sync",
        "feed-list",
    ):
        await feed_admin.run(
            container,
            Namespace(
                command=command,
                program=p.id,
                query="Sony",
                dry_run=True,
                confirm=False,
                network="awin",
            ),
        )
        printed = capsys.readouterr().out
        assert "awin1.com" not in printed and "affiliate_url" not in printed
    data = tmp_path / "program.json"
    data.write_text(
        '{"network":"awin","external_merchant_id":"2","external_feed_id":"2","market_country":"BE","display_name":"Example","domain":"example.com","currency":"EUR"}'
    )
    await feed_admin.run(
        container,
        Namespace(command="merchant-program-import", file=data, dry_run=False, confirm=True),
    )
    assert '"active": false' in capsys.readouterr().out
    container.feed_sources = {}
    with pytest.raises(FeedError, match="network_disabled"):
        await feed_admin.run(container, Namespace(command="feed-sync", program=p.id, dry_run=True))


async def test_feed_startup_requires_credentials_and_approved_programs(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "1")
    settings = container.settings.model_copy(update={"feed_program_ids": [], "awin_enabled": True})
    with pytest.raises(ValueError, match="FEED_PROGRAM_IDS"):
        Container(settings, sessions=container.sessions, redis=container.redis)
    settings.feed_program_ids = [str(p.id)]
    with pytest.raises(ValueError, match="AWIN_FEED_API_KEY"):
        Container(settings, sessions=container.sessions, redis=container.redis)
    settings.awin_feed_api_key = SecretStr("fixture-secret")
    resources = Container(settings, sessions=container.sessions, redis=container.redis)
    await resources.validate_feeds()
    resources.settings.feed_program_ids = [str(uuid4())]
    with pytest.raises(ValueError, match="approved programs"):
        await resources.validate_feeds()
    await resources.http.aclose()
    if resources.payment_provider.bot:
        await resources.payment_provider.bot.session.close()
