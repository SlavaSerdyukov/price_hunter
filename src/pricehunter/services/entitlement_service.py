from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from pricehunter.db.base import utcnow
from pricehunter.db.models import Subscription, SubscriptionPeriod, Tracker
from pricehunter.db.session import SessionFactory
from pricehunter.domain.subscriptions import (
    EffectiveEntitlement,
    Plan,
    SubscriptionPolicy,
    SubscriptionStatus,
)


class EntitlementService:
    """Subscription records are the only paid-access authority, including in SQL scheduling."""

    def __init__(self, sessions: SessionFactory, policy: SubscriptionPolicy) -> None:
        self.sessions, self.policy = sessions, policy

    @staticmethod
    def active_at(now: datetime) -> ColumnElement[bool]:
        return and_(
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED]),
            Subscription.valid_from <= now,
            Subscription.valid_until > now,
            or_(
                Subscription.checkout_intent_id.is_(None),  # Preserved legacy subscription.
                exists().where(
                    SubscriptionPeriod.subscription_id == Subscription.id,
                    SubscriptionPeriod.refunded_at.is_(None),
                    SubscriptionPeriod.valid_from <= now,
                    SubscriptionPeriod.valid_until > now,
                ),
            ),
        )

    def plan_expression(self, now: datetime) -> ColumnElement[str]:
        """Same policy as for_user, correlated with a Tracker's user, no stale plan cache."""
        return case(
            *[
                (
                    exists()
                    .where(
                        Subscription.user_id == Tracker.user_id,
                        Subscription.plan == plan,
                        self.active_at(now),
                    )
                    .correlate(Tracker),
                    str(plan),
                )
                for plan in (Plan.POWER, Plan.PRO)
            ],
            else_=str(Plan.FREE),
        )

    def scheduled_tracker_ids(self, now: datetime) -> Select[tuple[UUID]]:
        plan = self.plan_expression(now)
        limit = case(
            *[(plan == p, self.policy.for_plan(p).max_trackers) for p in Plan],
            else_=0,
        )
        ranked = (
            select(
                Tracker.id,
                func.row_number()
                .over(partition_by=Tracker.user_id, order_by=(Tracker.created_at, Tracker.id))
                .label("position"),
                limit.label("quota"),
            )
            .where(Tracker.enabled.is_(True))
            .subquery()
        )
        return select(ranked.c.id).where(ranked.c.position <= ranked.c.quota)

    async def for_user(
        self,
        user_id: UUID,
        *,
        session: AsyncSession | None = None,
        now: datetime | None = None,
    ) -> EffectiveEntitlement:
        if session is None:
            async with self.sessions() as own:
                return await self.for_user(user_id, session=own, now=now)
        now = now or utcnow()
        subscription = await session.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                self.active_at(now),
            )
            .order_by(
                case((Subscription.plan == Plan.POWER, 2), else_=1).desc(),
                Subscription.valid_until.desc(),
                Subscription.id,
            )
            .limit(1)
        )
        if subscription is None:
            latest = await session.scalar(
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                )
                .order_by(Subscription.valid_until.desc())
                .limit(1)
            )
            status = (
                "free"
                if latest is None
                else ("refunded" if latest.status == SubscriptionStatus.REFUNDED else "expired")
            )
            return EffectiveEntitlement(Plan.FREE, self.policy.for_plan(Plan.FREE), status)
        # A refunded gap must not be displayed as continuous access until a future renewal.
        until = subscription.valid_until
        if subscription.checkout_intent_id is not None:
            periods = list(
                await session.scalars(
                    select(SubscriptionPeriod)
                    .where(
                        SubscriptionPeriod.subscription_id == subscription.id,
                        SubscriptionPeriod.refunded_at.is_(None),
                        SubscriptionPeriod.valid_until > now,
                    )
                    .order_by(SubscriptionPeriod.valid_from)
                )
            )
            until = now
            for period in periods:
                if period.valid_from > until:
                    break
                until = max(until, period.valid_until)
        plan = Plan(subscription.plan)
        return EffectiveEntitlement(
            plan,
            self.policy.for_plan(plan),
            subscription.status,
            until,
            subscription.id,
            subscription.auto_renew,
        )

    async def counts(self, user_id: UUID) -> tuple[int, int]:
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(Tracker)
                .where(
                    Tracker.user_id == user_id,
                )
            )
            active = await session.scalar(
                select(func.count())
                .select_from(Tracker)
                .where(
                    Tracker.user_id == user_id,
                    Tracker.id.in_(self.scheduled_tracker_ids(utcnow())),
                )
            )
            return total or 0, active or 0
