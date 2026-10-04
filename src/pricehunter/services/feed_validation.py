"""Read-only acquisition with immutable evidence, independent of activation."""

import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import FeedSyncState, MerchantProgram, MerchantProgramValidation
from pricehunter.db.session import SessionFactory
from pricehunter.domain.feeds import FeedError, FeedReport, RejectedFeedRow
from pricehunter.domain.provider_policy import ProviderDataPolicy
from pricehunter.providers.feeds.base import FeedSource
from pricehunter.services.feed_quality import (
    FeedQualityEvaluator,
    configuration_fingerprint,
    version_digest,
)

ADAPTER_REVISION = "m4e-feed-v1"


def reviewed_policy(program: MerchantProgram) -> ProviderDataPolicy:
    policy = ProviderDataPolicy.model_validate(program.policy_data)
    if not program.approved or not program.last_reviewed_at or not policy.reviewed:
        raise ValueError("Program needs explicit review and approval")
    policy.require("catalog_persistence_allowed")
    return policy


async def require_validation(
    session: AsyncSession, program: MerchantProgram, settings: Settings
) -> None:
    # Latest result for this exact configuration wins, including a failed rerun.
    report = await session.scalar(
        select(MerchantProgramValidation)
        .where(
            MerchantProgramValidation.merchant_program_id == program.id,
            MerchantProgramValidation.configuration_fingerprint
            == configuration_fingerprint(program),
        )
        .order_by(
            MerchantProgramValidation.completed_at.desc(), MerchantProgramValidation.id.desc()
        )
        .limit(1)
    )
    if (
        report is None
        or report.status != "passed"
        or report.adapter_revision != ADAPTER_REVISION
        or report.completed_at > utcnow()
        or report.completed_at
        < utcnow() - timedelta(seconds=settings.merchant_validation_max_age_seconds)
    ):
        raise ValueError("Current passed technical validation is required")


def validation_summary(
    report: MerchantProgramValidation, program: MerchantProgram | None = None
) -> dict[str, Any]:
    result = {
        key: getattr(report, key)
        for key in (
            "id",
            "merchant_program_id",
            "status",
            "configuration_fingerprint",
            "source_version",
            "started_at",
            "completed_at",
            "valid_rows",
            "invalid_rows",
            "duplicate_rows",
            "sampled_rows",
            "warnings",
            "metrics",
            "error_code",
            "adapter_revision",
            "created_at",
        )
    }
    result["age_seconds"] = max(0, int((utcnow() - report.completed_at).total_seconds()))
    if program:
        result["fingerprint_matches"] = (
            report.configuration_fingerprint == configuration_fingerprint(program)
        )
    return result


class FeedValidationService:
    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings
        self.quality = FeedQualityEvaluator(settings)

    async def run(self, program_id: UUID, source: FeedSource) -> MerchantProgramValidation:
        async with self.sessions.begin() as session:
            # Transaction-scoped lock releases even after cancellation/connection loss.
            # No catalog or program row lock is held during HTTP streaming.
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "feed-validation:" + str(program_id)},
            )
            if not locked:
                raise FeedError("validation_busy")
            program = await session.get(MerchantProgram, program_id)
            if program is None or program.network != source.name:
                raise FeedError("unknown_program")
            reviewed_policy(program)
            fingerprint, started = configuration_fingerprint(program), utcnow()
            report, version, error = FeedReport(), None, None
            try:
                if program.feed_mode != "full":
                    raise FeedError("unsupported_feed_mode")
                version = version_digest(await source.source_version(program))
                with tempfile.TemporaryDirectory(prefix="pricehunter-validation-") as directory:
                    scratch = sqlite3.connect(Path(directory) / "ids.sqlite")
                    try:
                        scratch.execute("PRAGMA cache_size=-1024")
                        scratch.execute(
                            "CREATE TABLE items (id TEXT PRIMARY KEY, fingerprint TEXT)"
                        )
                        async for item in source.stream_items(program):
                            self.quality.count(report, item, program)
                            if isinstance(item, RejectedFeedRow):
                                continue
                            item_hash = item.fingerprint()
                            added = scratch.execute(
                                "INSERT OR IGNORE INTO items VALUES (?, ?)",
                                (item.external_id, item_hash),
                            ).rowcount
                            report.duplicates += not added
                            if (
                                not added
                                and scratch.execute(
                                    "SELECT fingerprint FROM items WHERE id=?", (item.external_id,)
                                ).fetchone()[0]
                                != item_hash
                            ):
                                raise FeedError("conflicting_duplicate")
                    finally:
                        scratch.close()
                if version != version_digest(await source.source_version(program)):
                    raise FeedError("source_changed_during_sync")
                self.quality.evaluate(report)
            except Exception as exc:
                error = exc.code if isinstance(exc, FeedError) else "technical_validation_failed"
            # Refresh and serialize completion with operator configuration/activation.
            await session.get(FeedSyncState, program_id, with_for_update=True)
            current = await session.get(
                MerchantProgram, program_id, with_for_update=True, populate_existing=True
            )
            assert current is not None
            if fingerprint != configuration_fingerprint(current):
                error = "validation_configuration_changed"
            try:
                reviewed_policy(current)
            except Exception:
                # Rights might have been revoked while acquisition was in progress.
                error = "validation_rights_changed"
            report.completed_at = utcnow()
            if error:
                report.failure_kind = "technical_validation_failed"
            evidence = MerchantProgramValidation(
                merchant_program_id=program_id,
                status="failed" if error else "passed",
                configuration_fingerprint=fingerprint,
                source_version=version,
                started_at=started,
                completed_at=report.completed_at,
                valid_rows=report.valid_rows,
                invalid_rows=report.invalid_rows,
                duplicate_rows=report.duplicates,
                sampled_rows=0,
                warnings=report.warnings,
                metrics=report.model_dump(mode="json"),
                error_code=error,
                adapter_revision=ADAPTER_REVISION,
            )
            session.add(evidence)
            await session.flush()
            return evidence

    async def history(self, program_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            program = await session.get(MerchantProgram, program_id)
            rows = await session.scalars(
                select(MerchantProgramValidation)
                .where(MerchantProgramValidation.merchant_program_id == program_id)
                .order_by(MerchantProgramValidation.completed_at.desc())
                .limit(min(200, max(1, limit)))
            )
            return [validation_summary(row, program) for row in rows]

    async def show(self, validation_id: UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            report = await session.get(MerchantProgramValidation, validation_id)
            if report is None:
                raise ValueError("Unknown technical validation")
            return validation_summary(
                report, await session.get(MerchantProgram, report.merchant_program_id)
            )
