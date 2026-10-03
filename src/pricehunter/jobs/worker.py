from datetime import UTC
from typing import Any, cast
from uuid import UUID

import structlog
from arq import cron
from arq.connections import RedisSettings
from arq.worker import func
from sqlalchemy import exists, select

from pricehunter.bot.app import create_bot
from pricehunter.bot.sender import TelegramNotificationSender
from pricehunter.core.config import get_settings
from pricehunter.core.container import Container
from pricehunter.core.logging import configure_logging
from pricehunter.db.models import FeedSyncState, MerchantProgram, StoreOffer
from pricehunter.providers.http import ProviderHTTP
from pricehunter.services.catalog_policy_maintenance import CatalogPolicyMaintenance
from pricehunter.services.discovery_service import DiscoveryClaim
from pricehunter.services.feed_materialization import FeedMaterializationService
from pricehunter.services.notification_service import NotificationService
from pricehunter.services.price_check_service import RefreshClaim


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = Container(settings)
    await container.validate_feeds()
    ctx["container"] = container
    if settings.telegram_bot_token.get_secret_value():
        bot = create_bot(container)
        ctx["bot"] = bot
        ctx["notifications"] = NotificationService(
            container.sessions, TelegramNotificationSender(bot), container.entitlements, settings
        )


async def shutdown(ctx: dict[str, Any]) -> None:
    if "bot" in ctx:
        await ctx["bot"].session.close()
    await ctx["container"].close()


async def refresh_due_offers(ctx: dict[str, Any]) -> int:
    container = cast(Container, ctx["container"])
    claims = await container.price_checks.claim_due()
    for claim in claims:
        try:
            await ctx["redis"].enqueue_job(
                "refresh_offer",
                str(claim.offer_id),
                str(claim.token),
                _job_id=f"refresh:{claim.token}",
            )
        except Exception:
            # The database lease makes a lost enqueue recoverable after expiry.
            structlog.get_logger().warning("enqueue_failed", store_offer_id=str(claim.offer_id))
    return len(claims)


async def refresh_offer(ctx: dict[str, Any], offer_id: str, token: str) -> bool:
    container = cast(Container, ctx["container"])
    with structlog.contextvars.bound_contextvars(job_id=ctx.get("job_id"), store_offer_id=offer_id):
        return await container.price_checks.refresh(RefreshClaim(UUID(offer_id), UUID(token)))


async def send_notifications(ctx: dict[str, Any]) -> int:
    if "notifications" not in ctx:
        return 0  # Leave pending events durable until delivery is configured.
    return await cast(NotificationService, ctx["notifications"]).send_pending(limit=100)


async def maintain_billing(ctx: dict[str, Any]) -> int:
    container = cast(Container, ctx["container"])
    retried = await container.billing_intake.retry_pending()
    await container.billing.expire()
    return retried


async def discover_products(ctx: dict[str, Any]) -> int:
    container = cast(Container, ctx["container"])
    await container.discovery.synchronize()
    claims = await container.discovery.claim_due()
    for claim in claims:
        try:
            await ctx["redis"].enqueue_job(
                "discover_product",
                str(claim.target_id),
                str(claim.token),
                _job_id=f"discovery:{claim.token}",
            )
        except Exception:
            structlog.get_logger().warning(
                "discovery_enqueue_failed", target_id=str(claim.target_id)
            )
    return len(claims)


async def discover_product(ctx: dict[str, Any], target_id: str, token: str) -> bool:
    return await cast(Container, ctx["container"]).discovery.discover(
        DiscoveryClaim(UUID(target_id), UUID(token))
    )


async def maintain_comparisons(ctx: dict[str, Any]) -> int:
    return await cast(Container, ctx["container"]).comparison_operations.maintain()


async def maintain_commerce(ctx: dict[str, Any]) -> bool:
    container = cast(Container, ctx["container"])
    await container.outbound.retain(container.sessions)
    await CatalogPolicyMaintenance(container.sessions, container.settings).purge()
    return await container.fx.refresh(
        ProviderHTTP(container.http, timeout=20, max_bytes=100000), container.redis
    )


async def schedule_feeds(ctx: dict[str, Any]) -> int:
    container = cast(Container, ctx["container"])
    if not container.feed_sources:
        return 0
    await container.feed_sync.retain()
    count = 0
    for _ in range(min(10, container.settings.discovery_batch_size)):
        claim = await container.feed_sync.claim()
        if claim is None:
            break
        program_id, token = claim
        try:
            await ctx["redis"].enqueue_job(
                "sync_feed", str(program_id), str(token), _job_id=f"feed:{token}"
            )
            count += 1
        except Exception:
            structlog.get_logger().warning("feed_enqueue_failed", program_id=str(program_id))
    # Refresh only bounded materialized work; staging rows never get individual jobs.
    async with container.sessions() as session:
        ids = list(
            await session.scalars(
                select(MerchantProgram.id)
                .where(
                    MerchantProgram.id.in_([UUID(v) for v in container.settings.feed_program_ids]),
                    MerchantProgram.active.is_(True),
                    exists().where(
                        StoreOffer.merchant_program_id == MerchantProgram.id,
                        FeedSyncState.merchant_program_id == MerchantProgram.id,
                        StoreOffer.feed_generation < FeedSyncState.generation,
                    ),
                )
                .order_by(MerchantProgram.id)
                .limit(container.settings.feed_batch_size)
            )
        )
    for program_id in ids:
        await FeedMaterializationService(container.products).refresh(program_id, limit=10)
    return count


async def sync_feed(ctx: dict[str, Any], program_id: str, token: str) -> bool:
    container = cast(Container, ctx["container"])
    program = await container.merchant_programs.get(UUID(program_id))
    source = container.feed_sources.get(program.network)
    if source is None:
        return False
    await container.feed_sync.run(program.id, source, token=UUID(token))
    return True


class WorkerSettings:
    timezone = UTC
    functions = [
        schedule_feeds,
        func(sync_feed, timeout=7200),
        refresh_offer,
        refresh_due_offers,
        send_notifications,
        maintain_billing,
        discover_product,
        discover_products,
        maintain_comparisons,
        maintain_commerce,
    ]
    cron_jobs = [
        cron(schedule_feeds, second=45, run_at_startup=True, unique=True),
        cron(
            maintain_commerce, minute={0, 15, 30, 45}, second=40, run_at_startup=True, unique=True
        ),
        cron(discover_products, second=20, run_at_startup=True, unique=True),
        cron(maintain_comparisons, second={2, 32}, run_at_startup=True, unique=True),
        cron(maintain_billing, second=10, run_at_startup=True, unique=True),
        cron(refresh_due_offers, second={0, 30}, run_at_startup=True, unique=True),
        cron(send_notifications, second={5, 15, 25, 35, 45, 55}, run_at_startup=True, unique=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())
    max_jobs = 10
    job_timeout = 60
    keep_result = 120
    max_tries = 3
    health_check_interval = 30
