import asyncio
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import (
    DateTime,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import FeedPendingItem, FeedSyncState, MerchantFeedItem, MerchantProgram
from pricehunter.db.session import SessionFactory
from pricehunter.domain.feeds import FeedError, FeedProductData, FeedReport, RejectedFeedRow
from pricehunter.domain.products import model_code, normalized
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.services.feed_materialization import FeedMaterializationService
from pricehunter.services.policy_resolver import PolicyResolver
from pricehunter.services.product_service import ProductService


class FeedSyncService:
    def __init__(
        self, sessions: SessionFactory, settings: Settings, products: ProductService
    ) -> None:
        self.sessions, self.settings, self.products = sessions, settings, products

    def _next_sync_at(self, program: MerchantProgram, confirmed_at: datetime) -> datetime:
        ttl = PolicyResolver(self.settings).program(program).max_cache_seconds
        return min(
            utcnow() + timedelta(seconds=self.settings.feed_sync_seconds),
            confirmed_at + timedelta(seconds=ttl),
        )

    async def claim(self, program_id: UUID | None = None) -> tuple[UUID, UUID] | None:
        async with self.sessions.begin() as session:
            state = await session.scalar(
                select(FeedSyncState)
                .join(MerchantProgram)
                .where(
                    PolicyResolver(self.settings).program_filter("catalog_persistence_allowed"),
                    FeedSyncState.merchant_program_id == program_id
                    if program_id
                    else FeedSyncState.next_sync_at <= utcnow(),
                    or_(FeedSyncState.lease_until.is_(None), FeedSyncState.lease_until <= utcnow()),
                )
                .order_by(FeedSyncState.next_sync_at, FeedSyncState.merchant_program_id)
                .limit(1)
                .with_for_update(of=FeedSyncState, skip_locked=True)
            )
            if state is None:
                return None
            state.lease_token = uuid4()
            state.lease_until = utcnow() + timedelta(seconds=self.settings.feed_lease_seconds)
            state.started_at, state.status = utcnow(), "queued"
            return state.merchant_program_id, state.lease_token

    async def _fence(self, session: AsyncSession, program_id: UUID, token: UUID) -> FeedSyncState:
        state = await session.get(FeedSyncState, program_id, with_for_update=True)
        if (
            state is None
            or state.lease_token != token
            or state.lease_until is None
            or state.lease_until <= utcnow()
        ):
            raise FeedError("lease_lost")
        program = await session.get(MerchantProgram, program_id, with_for_update=True)
        PolicyResolver(self.settings).program(program).require("catalog_persistence_allowed")
        state.lease_until = utcnow() + timedelta(seconds=self.settings.feed_lease_seconds)
        return state

    @asynccontextmanager
    async def _heartbeat(self, program_id: UUID, token: UUID) -> AsyncIterator[None]:
        async def pulse() -> None:
            while True:
                await asyncio.sleep(self.settings.feed_lease_seconds / 3)
                async with self.sessions.begin() as session:
                    await self._fence(session, program_id, token)

        task = asyncio.create_task(pulse())
        try:
            yield
            if task.done():
                await task
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run(
        self,
        program_id: UUID,
        source: FeedSource,
        *,
        dry_run: bool = False,
        token: UUID | None = None,
    ) -> FeedReport:
        async with self.sessions() as session:
            program = await session.get(MerchantProgram, program_id)
            if program is None or source.name != program.network:
                raise FeedError("unknown_program")
            PolicyResolver(self.settings).program(program).require("catalog_persistence_allowed")
        if dry_run:
            return await self._dry_run(program, source)
        if token is None:
            claim = await self.claim(program_id)
            if claim is None:
                raise FeedError("sync_busy")
            _, token = claim
        async with self.sessions.begin() as session:
            claimed_state = await self._fence(session, program_id, token)
            if claimed_state.status != "queued":
                raise FeedError("sync_busy")
            claimed_state.status = "fetching"
        report = FeedReport()
        log = structlog.get_logger().bind(
            network=program.network, program_id=str(program_id), market=program.market_country
        )
        log.info("feed_sync_started")
        try:
            async with self._heartbeat(program_id, token):
                version = await source.source_version(program)
                async with self.sessions() as session:
                    state = await session.get(FeedSyncState, program_id)
                    current_program = await session.get(MerchantProgram, program_id)
                    cutoff = utcnow() - timedelta(
                        seconds=PolicyResolver(self.settings)
                        .program(current_program)
                        .max_cache_seconds
                    )
                    unchanged = bool(
                        version
                        and state
                        and state.source_version == version
                        and state.last_success_at
                        and state.last_success_at > cutoff
                    )
                    if unchanged:
                        assert state is not None
                        retained = await session.scalar(
                            select(func.count())
                            .select_from(MerchantFeedItem)
                            .where(
                                MerchantFeedItem.merchant_program_id == program_id,
                                MerchantFeedItem.active.is_(True),
                                MerchantFeedItem.feed_generation == state.generation,
                                MerchantFeedItem.seen_at > cutoff,
                            )
                        )
                        unchanged = retained == state.row_count if state else False
                if not unchanged:
                    batch: list[dict[str, Any]] = []
                    async for item in source.stream_items(program):
                        self._count(report, item, program)
                        if isinstance(item, RejectedFeedRow):
                            if report.invalid_rows <= 10:
                                log.info("feed_item_rejected", code=item.code)
                            continue
                        batch.append(self._pending(program_id, token, item))
                        if len(batch) >= self.settings.feed_batch_size:
                            await self._write(program_id, token, batch, report)
                            batch = []
                    if batch:
                        await self._write(program_id, token, batch, report)
                    if version != await source.source_version(program):
                        raise FeedError("source_changed_during_sync")
            if unchanged:
                async with self.sessions.begin() as session:
                    state = await self._fence(session, program_id, token)
                    state.status, state.lease_token, state.lease_until = "complete", None, None
                    state.failure_count, state.error_code = 0, None
                    current_program = await session.get(MerchantProgram, program_id)
                    assert current_program is not None and state.last_success_at is not None
                    state.next_sync_at = self._next_sync_at(current_program, state.last_success_at)
                return FeedReport.model_validate(state.report)
            await self._complete(program, token, version, report)
            log.info("feed_generation_completed", rows=report.valid_rows)
        except BaseException as exc:
            async with self.sessions.begin() as session:
                state = await session.get(FeedSyncState, program_id, with_for_update=True)
                if state and state.lease_token == token:
                    state.failure_count += 1
                    state.last_failure_at = utcnow()
                    state.error_code = exc.code if isinstance(exc, FeedError) else "sync_failed"
                    state.status, state.lease_token, state.lease_until = "failed", None, None
                    state.next_sync_at = utcnow() + timedelta(
                        seconds=min(86400, 300 * 2 ** min(state.failure_count, 8))
                    )
            log.warning(
                "feed_sync_failed", code=exc.code if isinstance(exc, FeedError) else "sync_failed"
            )
            raise
        await FeedMaterializationService(self.products).refresh(program_id)
        log.info("feed_sync_completed", rows=report.valid_rows, invalid=report.invalid_rows)
        return report

    def _count(
        self, report: FeedReport, item: FeedProductData | RejectedFeedRow, program: MerchantProgram
    ) -> None:
        report.rows_parsed += 1
        if report.rows_parsed > self.settings.feed_max_rows:
            raise FeedError("row_limit")
        if isinstance(item, RejectedFeedRow):
            report.invalid_rows += 1
            return
        if item.currency != program.currency:
            raise FeedError("wrong_currency")
        if item.source_updated_at and item.source_updated_at > utcnow():
            raise FeedError("future_source_version")
        report.valid_rows += 1
        report.identifier_coverage += bool(item.gtin or item.ean or item.upc or item.mpn)
        report.variant_coverage += bool(item.variant)
        report.availability_coverage += item.availability != "unknown"
        report.currencies[item.currency] = report.currencies.get(item.currency, 0) + 1

    @staticmethod
    def _pending(program_id: UUID, token: UUID, item: FeedProductData) -> dict[str, Any]:
        brand = model_code(item.brand or "")
        return {
            "merchant_program_id": program_id,
            "attempt": token,
            "external_id": item.external_id,
            "data": item.model_dump(mode="json"),
            "fingerprint": item.fingerprint(),
            "gtin": next((v.zfill(14) for v in (item.gtin, item.ean, item.upc) if v), None),
            "brand_model": f"{brand}:{model_code(item.model)}" if brand and item.model else None,
            "brand_mpn": f"{brand}:{model_code(item.mpn)}" if brand and item.mpn else None,
            "normalized_title": normalized(item.title),
        }

    async def _write(
        self, program_id: UUID, token: UUID, batch: list[dict[str, Any]], report: FeedReport
    ) -> None:
        async with self.sessions.begin() as session:
            await self._fence(session, program_id, token)
            result = await session.scalars(
                insert(FeedPendingItem)
                .values(batch)
                .on_conflict_do_nothing()
                .returning(FeedPendingItem.external_id)
            )
            report.duplicates += len(batch) - len(result.all())
            saved: dict[str, str] = {
                key: value
                for key, value in (
                    await session.execute(
                        select(FeedPendingItem.external_id, FeedPendingItem.fingerprint).where(
                            FeedPendingItem.merchant_program_id == program_id,
                            FeedPendingItem.attempt == token,
                            FeedPendingItem.external_id.in_([r["external_id"] for r in batch]),
                        )
                    )
                ).all()
            }
            if any(saved[row["external_id"]] != row["fingerprint"] for row in batch):
                raise FeedError("conflicting_duplicate")

    async def _complete(
        self, program: MerchantProgram, token: UUID, version: str | None, report: FeedReport
    ) -> None:
        p, item = FeedPendingItem, MerchantFeedItem
        async with self.sessions.begin() as session:
            state = await self._fence(session, program.id, token)
            if report.valid_rows >= 1000:
                # Bulk staging can arrive before autovacuum updates statistics. Stale
                # tiny-table estimates otherwise select quadratic nested-loop plans.
                from sqlalchemy import text

                await session.execute(text("ANALYZE feed_pending_items"))
                await session.execute(text("ANALYZE merchant_feed_items"))
            generation, now = state.generation + 1, utcnow()
            pending = [p.merchant_program_id == program.id, p.attempt == token]
            matching = exists().where(*pending, p.external_id == item.external_id)
            report.would_insert = int(
                await session.scalar(
                    select(func.count())
                    .select_from(p)
                    .where(
                        *pending,
                        ~exists().where(
                            item.merchant_program_id == program.id,
                            item.external_id == p.external_id,
                        ),
                    )
                )
                or 0
            )
            report.would_update = int(
                await session.scalar(
                    select(func.count())
                    .select_from(p)
                    .join(
                        item,
                        (item.merchant_program_id == p.merchant_program_id)
                        & (item.external_id == p.external_id),
                    )
                    .where(*pending, p.fingerprint != item.fingerprint)
                )
                or 0
            )
            report.would_deactivate = int(
                await session.scalar(
                    select(func.count())
                    .select_from(item)
                    .where(item.merchant_program_id == program.id, item.active.is_(True), ~matching)
                )
                or 0
            )
            await session.execute(
                update(item)
                .where(item.merchant_program_id == program.id, item.active.is_(True), ~matching)
                .values(active=False, feed_generation=generation)
            )
            fields = [
                "merchant_program_id",
                "external_id",
                "data",
                "fingerprint",
                "gtin",
                "brand_model",
                "brand_mpn",
                "normalized_title",
            ]
            stmt = insert(item).from_select(
                ["id", *fields, "feed_generation", "seen_at", "active"],
                select(
                    func.gen_random_uuid(),
                    *(getattr(p, field) for field in fields),
                    literal(generation),
                    literal(now),
                    literal(True),
                ).where(*pending),
                include_defaults=False,
            )
            old_version = cast(item.data["source_updated_at"].as_string(), DateTime(timezone=True))
            new_version = cast(
                stmt.excluded.data["source_updated_at"].as_string(), DateTime(timezone=True)
            )
            newer = or_(
                old_version.is_(None), and_(new_version.is_not(None), new_version > old_version)
            )
            content_changed = and_(newer, item.fingerprint != stmt.excluded.fingerprint)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[item.merchant_program_id, item.external_id],
                    set_={
                        **{
                            field: case(
                                (content_changed, getattr(stmt.excluded, field)),
                                else_=getattr(item, field),
                            )
                            for field in fields[2:]
                        },
                        "feed_generation": generation,
                        # Presence is confirmed by the completed feed even if content is older.
                        "seen_at": now,
                        "active": True,
                    },
                )
            )
            report.completed_at = now
            state.generation, state.source_version, state.last_success_at = generation, version, now
            state.row_count = report.valid_rows - report.duplicates
            state.report, state.status, state.failure_count, state.error_code = (
                report.model_dump(mode="json"),
                "complete",
                0,
                None,
            )
            state.lease_token = state.lease_until = None
            current_program = await session.get(MerchantProgram, program.id)
            assert current_program is not None
            state.next_sync_at = self._next_sync_at(current_program, now)
            await session.execute(delete(p).where(*pending))

    async def _dry_run(self, program: MerchantProgram, source: FeedSource) -> FeedReport:
        report = FeedReport()
        version = await source.source_version(program)
        with tempfile.TemporaryDirectory(prefix="pricehunter-feed-") as directory:
            scratch = sqlite3.connect(Path(directory) / "dry-run.sqlite")
            try:
                scratch.execute("CREATE TABLE items (id TEXT PRIMARY KEY, fingerprint TEXT)")
                async for item in source.stream_items(program):
                    self._count(report, item, program)
                    if isinstance(item, RejectedFeedRow):
                        continue
                    added = scratch.execute(
                        "INSERT OR IGNORE INTO items VALUES (?, ?)",
                        (item.external_id, item.fingerprint()),
                    ).rowcount
                    report.duplicates += not added
                    if (
                        not added
                        and scratch.execute(
                            "SELECT fingerprint FROM items WHERE id=?", (item.external_id,)
                        ).fetchone()[0]
                        != item.fingerprint()
                    ):
                        raise FeedError("conflicting_duplicate")
                cursor = scratch.execute("SELECT id, fingerprint FROM items ORDER BY id")
                present_active = 0
                async with self.sessions() as session:
                    while rows := cursor.fetchmany(self.settings.feed_batch_size):
                        existing = {
                            r.external_id: r
                            for r in await session.scalars(
                                select(MerchantFeedItem).where(
                                    MerchantFeedItem.merchant_program_id == program.id,
                                    MerchantFeedItem.external_id.in_([r[0] for r in rows]),
                                )
                            )
                        }
                        for external_id, fingerprint in rows:
                            old = existing.get(external_id)
                            report.would_insert += old is None
                            report.would_update += (
                                old is not None and old.fingerprint != fingerprint
                            )
                            present_active += old is not None and old.active
                    active = await session.scalar(
                        select(func.count())
                        .select_from(MerchantFeedItem)
                        .where(
                            MerchantFeedItem.merchant_program_id == program.id,
                            MerchantFeedItem.active.is_(True),
                        )
                    )
                    report.would_deactivate = int(active or 0) - present_active
            finally:
                scratch.close()
        if version != await source.source_version(program):
            raise FeedError("source_changed_during_sync")
        return report

    async def retain(self) -> int:
        cutoff = utcnow() - timedelta(days=self.settings.feed_retention_days)
        async with self.sessions.begin() as session:
            pending_ids = (
                select(
                    FeedPendingItem.merchant_program_id,
                    FeedPendingItem.attempt,
                    FeedPendingItem.external_id,
                )
                .where(
                    FeedPendingItem.created_at < cutoff,
                    ~exists().where(
                        FeedSyncState.merchant_program_id == FeedPendingItem.merchant_program_id,
                        FeedSyncState.lease_token == FeedPendingItem.attempt,
                        FeedSyncState.lease_until > utcnow(),
                    ),
                )
                .limit(self.settings.feed_batch_size)
            )
            from sqlalchemy import tuple_

            await session.execute(
                delete(FeedPendingItem).where(
                    tuple_(
                        FeedPendingItem.merchant_program_id,
                        FeedPendingItem.attempt,
                        FeedPendingItem.external_id,
                    ).in_(pending_ids)
                )
            )
            p = MerchantProgram
            cache_expired = exists().where(
                p.id == MerchantFeedItem.merchant_program_id,
                p.policy_data["reviewed"].as_boolean().is_(True),
                p.policy_data["price_history_allowed"].as_boolean().is_(False),
                MerchantFeedItem.seen_at
                < utcnow()
                - func.make_interval(
                    0, 0, 0, 0, 0, 0, p.policy_data["max_cache_seconds"].as_integer()
                ),
            )
            eligible = or_(
                and_(MerchantFeedItem.active.is_(False), MerchantFeedItem.seen_at < cutoff),
                cache_expired,
            )
            candidates = (
                await session.execute(
                    select(MerchantFeedItem.id, MerchantFeedItem.merchant_program_id)
                    .where(eligible)
                    .limit(self.settings.feed_batch_size)
                )
            ).all()
            if not candidates:
                return 0
            # Same State -> Item order as completion; skip programs being published/fetched.
            locked_programs = list(
                await session.scalars(
                    select(FeedSyncState.merchant_program_id)
                    .where(
                        FeedSyncState.merchant_program_id.in_(
                            {r.merchant_program_id for r in candidates}
                        ),
                        or_(
                            FeedSyncState.lease_until.is_(None),
                            FeedSyncState.lease_until <= utcnow(),
                        ),
                    )
                    .order_by(FeedSyncState.merchant_program_id)
                    .with_for_update(skip_locked=True)
                )
            )
            removed = (
                await session.execute(
                    delete(MerchantFeedItem)
                    .where(
                        MerchantFeedItem.id.in_([r.id for r in candidates]),
                        MerchantFeedItem.merchant_program_id.in_(locked_programs),
                        eligible,
                    )
                    .returning(MerchantFeedItem.merchant_program_id, MerchantFeedItem.active)
                )
            ).all()
            reacquire = {program_id for program_id, active in removed if active}
            if reacquire:
                await session.execute(
                    update(FeedSyncState)
                    .where(FeedSyncState.merchant_program_id.in_(reacquire))
                    .values(next_sync_at=func.least(FeedSyncState.next_sync_at, utcnow()))
                )
            return len(removed)
