from datetime import UTC
from typing import Any, cast
from uuid import UUID

import structlog
from arq import cron
from arq.connections import RedisSettings

from pricehunter.bot.app import create_bot
from pricehunter.bot.sender import TelegramNotificationSender
from pricehunter.core.config import get_settings
from pricehunter.core.container import Container
from pricehunter.core.logging import configure_logging
from pricehunter.services.notification_service import NotificationService
from pricehunter.services.price_check_service import RefreshClaim


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = Container(settings)
    ctx["container"] = container
    if settings.telegram_bot_token.get_secret_value():
        bot = create_bot(container)
        ctx["bot"] = bot
        ctx["notifications"] = NotificationService(
            container.sessions, TelegramNotificationSender(bot), container.entitlements
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


class WorkerSettings:
    timezone = UTC
    functions = [refresh_offer, refresh_due_offers, send_notifications, maintain_billing]
    cron_jobs = [
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
