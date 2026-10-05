"""Deterministic lease-expiry rehearsals on synthetic databases only."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import select, update

from pricehunter.core.container import Container
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    FeedSyncState,
    MerchantProgram,
    MerchantProgramValidation,
    NotificationEvent,
    ProductDiscovery,
    StoreOffer,
)
from pricehunter.domain.feeds import FeedError, FeedProductData, RejectedFeedRow
from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.integrity import integrity
from pricehunter.operations.synthetic import Dataset, SyntheticFeed, require_synthetic
from pricehunter.services.notification_service import Delivery, NotificationService


class NeverSend:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, delivery: Delivery) -> int:
        self.calls += 1
        raise RecoveryError("synthetic_notification_must_not_send")


async def crash_rehearsal(container: Container, dataset: Dataset) -> dict[str, bool]:
    require_synthetic(container)
    past = utcnow() - timedelta(seconds=1)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == dataset.offer_id)
            .values(next_check_at=past, last_checked_at=utcnow() - timedelta(minutes=2))
        )
    refreshes = await container.price_checks.claim_due()
    old_refresh = next(c for c in refreshes if c.offer_id == dataset.offer_id)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == dataset.offer_id)
            .values(lease_until=past, next_check_at=past)
        )
    new_refresh = next(
        c for c in await container.price_checks.claim_due() if c.offer_id == dataset.offer_id
    )
    if (
        new_refresh.token == old_refresh.token
        or await container.price_checks.accept(old_refresh, dataset.snapshot)
        or not await container.price_checks.refresh(new_refresh)
    ):
        raise RecoveryError("refresh_fencing_failed")

    old_discovery = (await container.discovery.claim_due())[0]
    async with container.sessions.begin() as session:
        await session.execute(
            update(ProductDiscovery)
            .where(ProductDiscovery.id == old_discovery.target_id)
            .values(lease_until=past, next_discovery_at=past)
        )
    new_discovery = next(
        c for c in await container.discovery.claim_due() if c.target_id == old_discovery.target_id
    )
    if (
        new_discovery.token == old_discovery.token
        or await container.discovery.discover(old_discovery)
        or not await container.discovery.discover(new_discovery)
    ):
        raise RecoveryError("discovery_fencing_failed")

    async with container.sessions.begin() as session:
        await session.execute(
            update(FeedSyncState)
            .where(FeedSyncState.merchant_program_id == dataset.program_id)
            .values(next_sync_at=past)
        )
    first = await container.feed_sync.claim(dataset.program_id)
    assert first is not None
    async with container.sessions.begin() as session:
        await session.execute(
            update(FeedSyncState)
            .where(FeedSyncState.merchant_program_id == dataset.program_id)
            .values(lease_until=past)
        )
    successor = await container.feed_sync.claim(dataset.program_id)
    assert successor is not None
    if first[1] == successor[1]:
        raise RecoveryError("feed_fencing_failed")
    try:
        async with container.sessions.begin() as session:
            await container.feed_sync._fence(session, first[0], first[1])
    except FeedError as exc:
        if exc.code != "lease_lost":
            raise
    else:
        raise RecoveryError("feed_fencing_failed")
    await container.feed_sync.run(dataset.program_id, SyntheticFeed(), token=successor[1])

    entered, release = asyncio.Event(), asyncio.Event()

    class InterruptedValidation(SyntheticFeed):
        async def stream_items(
            self,
            program: MerchantProgram,
        ) -> AsyncIterator[FeedProductData | RejectedFeedRow]:
            entered.set()
            await release.wait()
            async for row in super().stream_items(program):
                yield row

    async with container.sessions() as session:
        evidence_before = set(await session.scalars(select(MerchantProgramValidation.id)))
    task = asyncio.create_task(
        container.feed_validation.run(dataset.program_id, InterruptedValidation())
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with container.sessions.begin() as session:
            await session.execute(
                update(FeedSyncState)
                .where(FeedSyncState.merchant_program_id == dataset.program_id)
                .values(validation_lease_until=past)
            )
        newer = await container.feed_validation.run(dataset.program_id, SyntheticFeed())
    finally:
        release.set()
    try:
        await task
    except FeedError as exc:
        if exc.code != "validation_lease_lost":
            raise
    else:
        raise RecoveryError("validation_fencing_failed")
    async with container.sessions() as session:
        evidence_after = set(await session.scalars(select(MerchantProgramValidation.id)))
    if evidence_after != evidence_before | {newer.id}:
        raise RecoveryError("validation_evidence_changed")

    sender = NeverSend()
    notifications = NotificationService(
        container.sessions, sender, container.entitlements, container.settings
    )
    delivery = await notifications.claim()
    if delivery is None:
        raise RecoveryError("notification_fixture_missing")
    async with container.sessions.begin() as session:
        await session.execute(
            update(NotificationEvent)
            .where(NotificationEvent.id == delivery.event_id)
            .values(attempt_started_at=utcnow() - timedelta(minutes=3))
        )
        # Other legitimate pending work is deliberately delayed to isolate this crash probe.
        await session.execute(
            update(NotificationEvent)
            .where(NotificationEvent.status == "pending")
            .values(available_at=utcnow() + timedelta(hours=1))
        )
    if await notifications.recover_stale() != 1:
        raise RecoveryError("notification_recovery_failed")
    async with container.sessions() as session:
        event = await session.get(NotificationEvent, delivery.event_id)
        assert event is not None
        if event.status != "uncertain":
            raise RecoveryError("notification_recovery_failed")
    if await notifications.send_pending() != 0 or sender.calls:
        raise RecoveryError("notification_uncertain_resent")
    await integrity(container.sessions)
    return {
        name: True
        for name in (
            "offer_refresh",
            "product_discovery",
            "feed_sync",
            "technical_validation",
            "notification_uncertain",
        )
    }
