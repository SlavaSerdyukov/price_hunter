"""Read-only local dependency checks shared by every runtime entrypoint."""

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.sql.elements import ColumnElement

from pricehunter.core.config import Settings
from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD
from pricehunter.db.base import Base, utcnow
from pricehunter.db.models import (
    BillingUpdate,
    FeedSyncState,
    MerchantProgram,
    MerchantProgramValidation,
    NotificationEvent,
    ProductDiscovery,
    StoreOffer,
)

if TYPE_CHECKING:
    from pricehunter.core.container import Container


class RuntimePreflightError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RuntimePreflight:
    def __init__(self, container: "Container") -> None:
        self.container = container

    async def schema_versions(self) -> list[str]:
        async with self.container.sessions() as session:
            return list(
                await session.scalars(text("SELECT version_num FROM alembic_version LIMIT 2"))
            )

    async def check_schema(self) -> None:
        try:
            versions = await self.schema_versions()
        except Exception:
            raise RuntimePreflightError("database_unavailable") from None
        if versions != [EXPECTED_SCHEMA_HEAD]:
            raise RuntimePreflightError("schema_mismatch")

    async def check_redis(self) -> None:
        try:
            if not await self.container.redis.ping():
                raise RuntimePreflightError("redis_unavailable")
        except Exception:
            raise RuntimePreflightError("redis_unavailable") from None

    async def check_feed_configuration(self) -> None:
        try:
            Settings.model_validate(self.container.settings.model_dump())
            await self.container.validate_feeds()
        except Exception:
            raise RuntimePreflightError("configuration_invalid") from None

    async def run(self) -> None:
        try:
            async with asyncio.timeout(self.container.settings.runtime_preflight_timeout_seconds):
                await self.check_schema()
                await self.check_redis()
                await self.check_feed_configuration()
        except TimeoutError:
            raise RuntimePreflightError("dependency_timeout") from None

    async def ready(self) -> bool:
        try:
            await self.run()
        except RuntimePreflightError as exc:
            structlog.get_logger().warning("runtime_not_ready", reason=exc.reason)
            return False
        return True


class RuntimeStatus:
    def __init__(self, container: "Container") -> None:
        self.container = container

    async def report(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": {"expected": EXPECTED_SCHEMA_HEAD, "current": [], "compatible": False},
            "redis": {"available": False},
        }
        preflight = RuntimePreflight(self.container)
        try:
            async with asyncio.timeout(self.container.settings.runtime_preflight_timeout_seconds):
                try:
                    versions = await preflight.schema_versions()
                    result["schema"].update(
                        current=versions, compatible=versions == [EXPECTED_SCHEMA_HEAD]
                    )
                except Exception:
                    result["reason"] = "database_unavailable"
                try:
                    await preflight.check_redis()
                    result["redis"]["available"] = True
                except RuntimePreflightError:
                    pass
                if result["schema"]["compatible"]:
                    result["counts"] = await self._counts()
        except TimeoutError:
            result["reason"] = "dependency_timeout"
        except Exception:
            result["reason"] = "diagnostics_unavailable"
        return result

    async def _counts(self) -> dict[str, Any]:
        now = utcnow()
        # One aggregate round trip per table; no customer records or tokens leave this service.
        groups: list[tuple[type[Base], dict[str, ColumnElement[bool]]]] = [
            (
                StoreOffer,
                {
                    "due_store_offers": StoreOffer.next_check_at <= now,
                    "leased_store_offers": StoreOffer.lease_until > now,
                    "expired_store_offer_leases": StoreOffer.lease_until <= now,
                },
            ),
            (
                ProductDiscovery,
                {
                    "due_discoveries": ProductDiscovery.next_discovery_at <= now,
                    "expired_discovery_leases": ProductDiscovery.lease_until <= now,
                },
            ),
            (
                NotificationEvent,
                {
                    "pending_notifications": NotificationEvent.status == "pending",
                    "uncertain_notifications": NotificationEvent.status == "uncertain",
                    "sending_notifications": NotificationEvent.status == "sending",
                },
            ),
            (BillingUpdate, {"pending_billing_updates": BillingUpdate.status == "pending"}),
            (
                FeedSyncState,
                {
                    "due_feed_syncs": FeedSyncState.next_sync_at <= now,
                    "failed_feed_syncs": FeedSyncState.failure_count > 0,
                    "expired_feed_sync_leases": FeedSyncState.lease_until <= now,
                    "expired_validation_leases": FeedSyncState.validation_lease_until <= now,
                },
            ),
            (MerchantProgram, {"active_merchant_programs": MerchantProgram.active.is_(True)}),
        ]
        counts: dict[str, Any] = {}
        async with self.container.sessions() as session:
            for model, conditions in groups:
                row = (
                    await session.execute(
                        select(
                            *(
                                func.count().filter(condition).label(name)
                                for name, condition in conditions.items()
                            )
                        ).select_from(model)
                    )
                ).one()
                counts.update(dict(row._mapping))
            rejected = await session.scalars(
                select(FeedSyncState.merchant_program_id)
                .where(
                    FeedSyncState.rejected_report["report"]["failure_kind"].astext
                    == "publication_quality_failed"
                )
                .order_by(FeedSyncState.last_failure_at.desc())
                .limit(10)
            )
            counts["last_quality_rejected_programs"] = [str(value) for value in rejected]
            # Stream programs in bounded batches: fingerprints are the exact M4E domain contract.
            from datetime import timedelta

            from pricehunter.services.feed_quality import configuration_fingerprint
            from pricehunter.services.feed_validation import ADAPTER_REVISION, require_validation

            fresh_after = now - timedelta(
                seconds=self.container.settings.merchant_validation_max_age_seconds
            )
            newest = (
                select(MerchantProgramValidation.id)
                .where(MerchantProgramValidation.merchant_program_id == MerchantProgram.id)
                .order_by(
                    MerchantProgramValidation.completed_at.desc(),
                    MerchantProgramValidation.id.desc(),
                )
                .limit(1)
                .correlate(MerchantProgram)
                .scalar_subquery()
            )
            stream = await session.stream(
                select(MerchantProgram, MerchantProgramValidation)
                .outerjoin(MerchantProgramValidation, MerchantProgramValidation.id == newest)
                .execution_options(yield_per=100)
            )
            stale = 0
            async for program, validation in stream:
                fingerprint = configuration_fingerprint(program)
                if validation is not None and validation.configuration_fingerprint != fingerprint:
                    # Configuration can return to a previously validated fingerprint.
                    try:
                        await require_validation(session, program, self.container.settings)
                    except ValueError:
                        stale += 1
                elif (
                    validation is None
                    or validation.status != "passed"
                    or validation.completed_at < fresh_after
                    or validation.completed_at > now
                    or validation.adapter_revision != ADAPTER_REVISION
                ):
                    stale += 1
            counts["programs_without_current_validation"] = stale
        return counts
