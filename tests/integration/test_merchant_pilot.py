"""M4E acceptance: exercise production services and migrated PostgreSQL."""

import asyncio
import json
import tracemalloc
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from pricehunter.db.models import (
    FeedPendingItem,
    FeedPublicationAudit,
    FeedSyncState,
    MerchantFeedItem,
    NotificationEvent,
    PriceObservation,
    Product,
    Store,
    StoreOffer,
)
from pricehunter.domain.feeds import FeedError, MerchantProgramInput, RejectedFeedRow
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.schemas.watches import WatchCreate
from pricehunter.services.notification_service import NotificationService
from tests.integration.test_commerce_feeds import row, setup_feeds, source
from tests.integration.test_merchant_lifecycle import link, source_pair
from tests.support import market_user

pytestmark = pytest.mark.integration


async def reviewed(container):
    p = await container.merchant_programs.save(
        MerchantProgramInput(
            network="awin",
            external_merchant_id="991",
            external_feed_id="991",
            market_country="BE",
            currency="EUR",
            domain="example.com",
            display_name="Pilot",
        )
    )
    return await container.merchant_programs.review(
        p.id,
        SYNTHETIC_POLICY,
        expected_version=p.version,
        reason="Fixture rights review",
    )


async def validation(container, p):
    from pricehunter.providers.feeds.base import FeedSource

    return await container.feed_validation.run(p.id, source(FeedSource, [row()]))


async def activated(container):
    await setup_feeds(container)
    p = await reviewed(container)
    await validation(container, p)
    return await container.merchant_programs.activate(
        p.id, expected_version=p.version, reason="Pilot"
    )


async def seed_generation(container, p, count):
    # Generate on the server, never construct a 100k Python list.
    async with container.sessions.begin() as s:
        await s.execute(
            text("""INSERT INTO merchant_feed_items
            (id,merchant_program_id,external_id,data,fingerprint,normalized_title,feed_generation,seen_at,active)
            SELECT gen_random_uuid(),:p, n::text, '{}'::jsonb,
                repeat('0',64), 'fixture',1,now(),true
            FROM generate_series(1,:count) n"""),
            {"p": p.id, "count": count},
        )
        state = await s.get(FeedSyncState, p.id)
        state.generation, state.row_count, state.source_version = 1, count, "published"
        from pricehunter.db.base import utcnow

        state.last_success_at = utcnow()


async def test_reviewed_unvalidated_cannot_activate(container):
    p = await reviewed(container)
    with pytest.raises(ValueError, match="validation"):
        await container.merchant_programs.activate(p.id, expected_version=p.version, reason="Pilot")


async def test_changed_feed_reference_invalidates_evidence(container):
    p = await reviewed(container)
    await validation(container, p)
    p = await container.merchant_programs.change_feed_reference(
        p.id, "992", expected_version=p.version, reason="New feed"
    )
    p = await container.merchant_programs.review(
        p.id, SYNTHETIC_POLICY, expected_version=p.version, reason="New rights"
    )
    with pytest.raises(ValueError, match="validation"):
        await container.merchant_programs.activate(p.id, expected_version=p.version, reason="Pilot")
    await validation(container, p)
    assert (
        await container.merchant_programs.activate(p.id, expected_version=p.version, reason="Pilot")
    ).active


@pytest.mark.parametrize(
    "previous,valid,invalid,error",
    [
        (100_000, 2_000, 0, "quality_shrink"),
        (10_000, 1_000, 9_000, "quality_invalid_ratio"),
    ],
)
async def test_complete_bad_candidate_preserves_published_generation(
    container, previous, valid, invalid, error
):
    from pricehunter.providers.feeds.base import FeedSource

    p = await activated(container)
    await seed_generation(container, p, previous)
    rows = (
        row(str(n)) if n < valid else RejectedFeedRow("invalid_row") for n in range(valid + invalid)
    )
    with pytest.raises(FeedError, match=error):
        await container.feed_sync.run(p.id, source(FeedSource, rows))
    async with container.sessions() as s:
        state = await s.get(FeedSyncState, p.id)
        assert (state.generation, state.row_count, state.source_version) == (
            1,
            previous,
            "published",
        )
        assert state.error_code == error and state.failure_count == 1
        assert (
            await s.scalar(
                select(func.count()).select_from(MerchantFeedItem).where(MerchantFeedItem.active)
            )
            == previous
        )
        assert await s.scalar(select(func.count()).select_from(FeedPendingItem)) == 0


async def test_shrink_override_is_consumed_once(container):
    from pricehunter.db.models import FeedPublicationAudit
    from pricehunter.providers.feeds.base import FeedSource

    p = await activated(container)
    await seed_generation(container, p, 100_000)

    def feed(count):
        return source(FeedSource, (row(str(n)) for n in range(count)))

    await container.feed_sync.run(
        p.id, feed(2_000), allow_shrink=True, reason="Reviewed migration", confirm=True
    )
    with pytest.raises(FeedError, match="quality_shrink"):
        await container.feed_sync.run(p.id, feed(20))
    async with container.sessions() as s:
        assert (await s.get(FeedSyncState, p.id)).generation == 2
        audit = (await s.scalars(select(FeedPublicationAudit))).one()
        assert audit.previous_rows == 100_000 and audit.candidate_rows == 2_000


async def test_pending_watch_cancelled_after_canonical_reassignment(container):
    a, b, stores = await source_pair(container)
    user = await market_user(container, 799991, "DE")
    await container.watches.create(user.id, WatchCreate(product_id=a.product_id, currency="EUR"))
    async with container.sessions.begin() as s:
        await s.execute(update(StoreOffer).where(StoreOffer.id == b.id).values(price=300))
        await container.watches.evaluate(s, a.product_id, market_country="DE")
    await link(container, stores)
    sender = AsyncMock()
    service = NotificationService(
        container.sessions, sender, container.entitlements, container.settings
    )
    assert await service.send_pending() == 0
    sender.send.assert_not_awaited()
    async with container.sessions() as s:
        assert (await s.scalars(select(NotificationEvent))).one().status == "cancelled"


async def test_inactive_validation_writes_only_evidence_and_never_grants_rights(container):
    p = await reviewed(container)
    before = await container.merchant_programs.history(p.id)
    result = await validation(container, p)
    assert result.status == "passed"
    current = await container.merchant_programs.get(p.id)
    assert not current.active and current.version == p.version
    assert current.policy_data == p.policy_data and current.approved
    assert len(await container.merchant_programs.history(p.id)) == len(before)
    async with container.sessions() as s:
        for model in (
            MerchantFeedItem,
            FeedPendingItem,
            StoreOffer,
            Product,
            PriceObservation,
            NotificationEvent,
        ):
            assert await s.scalar(select(func.count()).select_from(model)) == 0
        state = await s.get(FeedSyncState, p.id)
        assert state.generation == 0 and not state.report and state.lease_token is None
    history = await container.feed_validation.history(p.id)
    assert len(history) == 1 and history[0]["fingerprint_matches"]
    assert (await container.feed_validation.show(result.id))["status"] == "passed"
    with pytest.raises(ValueError, match="Unknown"):
        await container.feed_validation.show(uuid4())


async def test_unreviewed_validation_cannot_acquire_or_grant_permission(container):
    p = await reviewed(container)
    async with container.sessions.begin() as s:
        stored = await s.get(type(p), p.id)
        stored.approved = False
    with pytest.raises(ValueError, match="review"):
        await validation(container, p)
    assert not await container.feed_validation.history(p.id)


async def test_expired_validation_cannot_reactivate_but_rename_can(container, monkeypatch):
    p = await activated(container)
    p = await container.merchant_programs.disable(p.id, expected_version=p.version, reason="Pause")
    from pricehunter.db.base import utcnow

    late = utcnow() + timedelta(days=8)
    monkeypatch.setattr("pricehunter.services.feed_validation.utcnow", lambda: late)
    with pytest.raises(ValueError, match="validation"):
        await container.merchant_programs.activate(
            p.id, expected_version=p.version, reason="Resume"
        )
    await validation(container, p)
    async with container.sessions() as s:
        store = await s.get(Store, p.store_id)
    await container.merchants.change(
        store.merchant_id,
        expected_version=1,
        reason="Name",
        changes={"display_name": "Renamed"},
        confirm=True,
    )
    p = await container.merchant_programs.update_metadata(
        p.id, expected_version=p.version, display_name="Cosmetic", reason="Name"
    )
    assert (
        await container.merchant_programs.activate(
            p.id, expected_version=p.version, reason="Resume"
        )
    ).active


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_currency",
        "truncated_gzip",
        "invalid_schema",
        "http_401",
        "wrong_advertiser",
        "wrong_feed",
        "wrong_market",
        "compressed_size_limit",
        "page_limit",
    ],
)
async def test_systemic_validation_failure_is_safe_immutable_evidence(container, failure):
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)

    class Broken(FeedSource):
        name = "awin"

        async def stream_items(self, program):
            yield row()
            raise FeedError(failure)

    result = await container.feed_validation.run(p.id, Broken())
    assert result.status == "failed" and result.error_code == failure
    with pytest.raises(ValueError, match="validation"):
        await container.merchant_programs.activate(p.id, expected_version=p.version, reason="Pilot")
    async with container.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(FeedPendingItem)) == 0


async def test_raw_failure_text_and_rejection_categories_never_persist(container):
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)

    class Broken(FeedSource):
        name = "awin"

        async def source_version(self, program):
            return "https://feed.example/?token=PRIVATE"

        async def stream_items(self, program):
            for n in range(200):
                yield RejectedFeedRow(f"https://feed.example/?token=PRIVATE&row={n}")
            raise RuntimeError("PRIVATE credentials in upstream exception")

    result = await container.feed_validation.run(p.id, Broken())
    assert result.error_code == "technical_validation_failed"
    assert result.metrics["rejections"] == {"invalid_row": 200}
    assert "PRIVATE" not in json.dumps(await container.feed_validation.show(result.id), default=str)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE merchant_program_validations SET status='passed'",
        "DELETE FROM merchant_program_validations",
        "UPDATE feed_publication_audits SET reason='tamper'",
        "DELETE FROM feed_publication_audits",
    ],
)
async def test_pilot_evidence_is_append_only(container, statement):
    p = await activated(container)
    async with container.sessions.begin() as s:
        s.add(
            FeedPublicationAudit(
                merchant_program_id=p.id,
                generation=1,
                previous_rows=100,
                candidate_rows=1,
                invalid_rows=0,
                guard="quality_shrink",
                reason="Synthetic evidence",
            )
        )
    with pytest.raises(DBAPIError, match="append-only"):
        async with container.sessions.begin() as s:
            await s.execute(text(statement))


async def test_validation_concurrency_and_feed_change_race(container):
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(FeedSource):
        name = "awin"

        async def stream_items(self, program):
            entered.set()
            await release.wait()
            yield row()

    task = asyncio.create_task(container.feed_validation.run(p.id, Slow()))
    await entered.wait()
    try:
        with pytest.raises(FeedError, match="validation_busy"):
            await validation(container, p)
        # Operator changes do not wait for network acquisition; comparison reads work too.
        p = await container.merchant_programs.change_feed_reference(
            p.id, "992", expected_version=p.version, reason="Changed while validating"
        )
        p = await container.merchant_programs.review(
            p.id, SYNTHETIC_POLICY, expected_version=p.version, reason="Reviewed B"
        )
        with pytest.raises(ValueError, match="validation"):
            await container.merchant_programs.activate(
                p.id, expected_version=p.version, reason="Race"
            )
    finally:
        release.set()
    result = await task
    assert result.status == "failed" and result.error_code == "validation_configuration_changed"
    await validation(container, p)
    assert (
        await container.merchant_programs.activate(p.id, expected_version=p.version, reason="B")
    ).active


async def test_validation_100k_stream_has_bounded_python_memory(container):
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)
    template = row()

    class Large(FeedSource):
        name = "awin"

        async def stream_items(self, program):
            for n in range(100_000):
                yield template.model_copy(update={"external_id": str(n)})
                if n % 1000 == 0:
                    await asyncio.sleep(0)

    tracemalloc.start()
    try:
        result = await container.feed_validation.run(p.id, Large())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result.status == "passed" and result.valid_rows == 100_000
    assert peak < 32 * 1024 * 1024
    assert len(json.dumps(result.metrics)) < 8192


async def test_small_catalog_and_same_quality_dry_run(container):
    from pricehunter.providers.feeds.base import FeedSource

    p = await activated(container)

    def feed(n):
        return source(FeedSource, (row(str(i)) for i in range(n)))

    await container.feed_sync.run(p.id, feed(5))
    planned = await container.feed_sync.run(p.id, feed(3), dry_run=True)
    actual = await container.feed_sync.run(p.id, feed(3))
    assert planned.coverage == actual.coverage and planned.valid_ratio == actual.valid_ratio
    assert actual.would_deactivate == 2 and actual.drift["row_count_delta"] == -2


@pytest.mark.parametrize(
    "options",
    [
        {"allow_shrink": True},
        {"allow_shrink": True, "confirm": True},
        {
            "allow_shrink": True,
            "confirm": True,
            "reason": " ",
        },
        {"allow_shrink": True, "confirm": True, "reason": "Review", "dry_run": True},
    ],
)
async def test_override_requires_reason_and_confirmation(container, options):
    from pricehunter.providers.feeds.base import FeedSource

    p = await activated(container)
    with pytest.raises(ValueError, match="confirm"):
        await container.feed_sync.run(p.id, source(FeedSource, [row()]), **options)
    async with container.sessions() as s:
        assert (await s.get(FeedSyncState, p.id)).generation == 0


@pytest.mark.parametrize("change", ["rename", "legacy", "sent"])
async def test_watch_identity_compatibility(container, change):
    a, b, stores = await source_pair(container)
    user = await market_user(container, 799992, "DE")
    await container.watches.create(user.id, WatchCreate(product_id=a.product_id, currency="EUR"))
    async with container.sessions.begin() as s:
        await s.execute(update(StoreOffer).where(StoreOffer.id == b.id).values(price=300))
        await container.watches.evaluate(s, a.product_id, market_country="DE")
        event = (await s.scalars(select(NotificationEvent))).one()
        if change == "legacy":
            event.snapshot = {k: v for k, v in event.snapshot.items() if k != "merchant_id"}
        elif change == "sent":
            event.status = "sent"
        snapshot = event.snapshot.copy()
    if change == "rename":
        await container.merchants.change(
            stores[1].merchant_id,
            expected_version=1,
            reason="Name",
            changes={"display_name": "New name"},
            confirm=True,
        )
    else:
        await link(container, stores)
    sender = AsyncMock()
    sender.send.return_value = 42
    service = NotificationService(
        container.sessions, sender, container.entitlements, container.settings
    )
    assert await service.send_pending() == (0 if change == "sent" else 1)
    async with container.sessions() as s:
        event = (await s.scalars(select(NotificationEvent))).one()
        assert event.status == "sent" and event.snapshot == snapshot


async def test_failed_rerun_does_not_fall_back_to_older_pass(container):
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)
    first = await validation(container, p)
    failed = await container.feed_validation.run(p.id, source(FeedSource, [row(currency="USD")]))
    assert failed.status == "failed" and first.status == "passed"
    with pytest.raises(ValueError, match="validation"):
        await container.merchant_programs.activate(p.id, expected_version=p.version, reason="Pilot")
    assert len(await container.feed_validation.history(p.id)) == 2


async def test_validation_duplicate_conflict_and_changing_version_are_not_passes(container):
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)
    repeat = await container.feed_validation.run(p.id, source(FeedSource, [row(), row()]))
    assert repeat.status == "passed" and repeat.duplicate_rows == 1
    conflict = await container.feed_validation.run(
        p.id, source(FeedSource, [row(), row(price="1")])
    )
    assert conflict.error_code == "conflicting_duplicate"

    class Changed(FeedSource):
        name = "awin"
        calls = 0

        async def source_version(self, program):
            self.calls += 1
            return str(self.calls)

        async def stream_items(self, program):
            yield row()

    result = await container.feed_validation.run(p.id, Changed())
    assert result.error_code == "source_changed_during_sync"


async def test_rejected_candidate_keeps_materialized_price_stock_and_cache(container):
    from pricehunter.providers.feeds.base import FeedSource
    from pricehunter.providers.feeds.local import FeedStoreProvider

    p = await activated(container)
    await container.feed_sync.run(p.id, source(FeedSource, [row(str(n)) for n in range(10)]))
    provider = FeedStoreProvider("awin", container.sessions, container.settings)
    offer = await container.products.persist((await provider.search("Sony", country="BE"))[0])
    async with container.sessions() as s:
        stored = await s.get(StoreOffer, offer.id)
        checked, price, stock = stored.last_checked_at, stored.price, stored.availability
        observations = await s.scalar(select(func.count()).select_from(PriceObservation))
    bad = source(
        FeedSource, [row("0", price="1")] + [RejectedFeedRow("invalid_money") for _ in range(9)]
    )
    with pytest.raises(FeedError, match="quality_invalid_ratio"):
        await container.feed_sync.run(
            p.id, bad, allow_shrink=True, reason="Cannot override malformed feed", confirm=True
        )
    assert len(await provider.search("Sony", country="BE")) == 10
    async with container.sessions() as s:
        stored = await s.get(StoreOffer, offer.id)
        assert stored.catalog_active and (
            stored.last_checked_at,
            stored.price,
            stored.availability,
        ) == (checked, price, stock)
        assert await s.scalar(select(func.count()).select_from(PriceObservation)) == observations
        assert await s.scalar(select(func.count()).select_from(FeedPublicationAudit)) == 0
    detail = await container.coverage.program(p.id)
    assert detail["last_validation"]["fingerprint_matches"] and detail["generation"] == 1
    assert detail["rejected_report"]["report"]["failure_kind"] == "publication_quality_failed"


async def test_operator_validation_commands_work_while_network_disabled(container, capsys):
    import argparse

    from pricehunter.apps import feed_admin
    from pricehunter.providers.feeds.base import FeedSource

    p = await reviewed(container)
    assert not container.settings.awin_enabled
    container.feed_sources = {"awin": source(FeedSource, [row()])}
    parser = argparse.ArgumentParser()
    feed_admin.add_commands(parser.add_subparsers(dest="command"))
    await feed_admin.run(container, parser.parse_args(["merchant-program-validate", str(p.id)]))
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and not (await container.merchant_programs.get(p.id)).active
    for tokens in (
        ["merchant-program-validations", str(p.id)],
        ["merchant-program-validation-show", report["id"]],
        ["feed-diagnostics", str(p.id)],
    ):
        await feed_admin.run(container, parser.parse_args(tokens))
        assert "PRIVATE" not in capsys.readouterr().out
    container.feed_sources = {"awin": source(FeedSource, [row(currency="USD")])}
    with pytest.raises(FeedError, match="wrong_currency"):
        await feed_admin.run(container, parser.parse_args(["merchant-program-validate", str(p.id)]))
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
