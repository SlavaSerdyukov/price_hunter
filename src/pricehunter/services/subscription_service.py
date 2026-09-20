from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from pricehunter.core.config import Settings
from pricehunter.db.models import Subscription, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.subscriptions import Plan
from pricehunter.services.entitlement_service import EntitlementService


class SubscriptionView(BaseModel):
    plan: Plan
    status: str
    valid_until: datetime | None
    auto_renew: bool | None
    max_trackers: int
    tracker_limit: int
    tracker_count: int
    scheduled_tracker_count: int
    check_interval_seconds: int
    features: dict[str, bool]
    search_limit: int
    search_result_limit: int
    history_days: int
    checkout_available: bool


class RenewableSubscription(BaseModel):
    id: UUID
    plan: Plan
    valid_until: datetime


class SubscriptionService:
    def __init__(
        self, sessions: SessionFactory, entitlements: EntitlementService, settings: Settings
    ) -> None:
        self.sessions, self.entitlements, self.settings = sessions, entitlements, settings

    async def status(self, user_id: UUID) -> SubscriptionView:
        effective = await self.entitlements.for_user(user_id)
        total, active = await self.entitlements.counts(user_id)
        limits = effective.entitlements
        async with self.sessions() as session:
            user = await session.get(User, user_id)
            available = (
                self.settings.stars_billing_enabled
                and user is not None
                and user.telegram_user_id is not None
            )
        return SubscriptionView(
            plan=effective.plan,
            status=effective.status,
            valid_until=effective.valid_until,
            auto_renew=effective.auto_renew,
            max_trackers=limits.max_trackers,
            tracker_limit=limits.max_trackers,
            tracker_count=total,
            scheduled_tracker_count=active,
            check_interval_seconds=limits.check_interval_seconds,
            features={
                name: value for name, value in asdict(limits).items() if isinstance(value, bool)
            },
            search_limit=limits.search_limit,
            search_result_limit=limits.search_result_limit,
            history_days=limits.history_days,
            checkout_available=available,
        )

    async def renewable(self, user_id: UUID) -> list[RenewableSubscription]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.auto_renew.is_(True),
                    Subscription.provider == "telegram_stars",
                )
                .order_by(Subscription.created_at)
            )
            return [
                RenewableSubscription(id=row.id, plan=Plan(row.plan), valid_until=row.valid_until)
                for row in rows
            ]
