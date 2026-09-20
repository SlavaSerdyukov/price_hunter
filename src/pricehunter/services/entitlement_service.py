from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, case, exists, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from pricehunter.db.base import utcnow
from pricehunter.db.models import ProductWatch, Subscription, SubscriptionPeriod, Tracker
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

    def plan_expression(
        self, now: datetime, subject: type[Tracker] | type[ProductWatch] = Tracker
    ) -> ColumnElement[str]:
        """Same policy as for_user, correlated with a Tracker's user, no stale plan cache."""
        return case(
            *[
                (
                    exists()
                    .where(
                        Subscription.user_id == subject.user_id,
                        Subscription.plan == plan,
                        self.active_at(now),
                    )
                    .correlate(subject),
                    str(plan),
                )
                for plan in (Plan.POWER, Plan.PRO)
            ],
            else_=str(Plan.FREE),
        )

    def scheduled_tracker_ids(self, now: datetime) -> Select[tuple[UUID]]:
        return self.scheduled_ids(now)[0]

    def scheduled_watch_ids(self, now: datetime) -> Select[tuple[UUID]]:
        return self.scheduled_ids(now)[1]

    def scheduled_ids(self, now: datetime) -> tuple[Select[tuple[UUID]], Select[tuple[UUID]]]:
        # Compute the user's effective plan once. Expanding correlated subscription
        # checks for each subject/interval caused multi-second JIT compilation in PostgreSQL.
        paid = (
            select(
                Subscription.user_id,
                func.max(case((Subscription.plan == Plan.POWER, 2), else_=1)).label("rank"),
            )
            .where(self.active_at(now))
            .group_by(Subscription.user_id)
            .subquery()
        )
        combined = union_all(
            *[
                select(
                    model.id, model.user_id, model.created_at, literal(kind).label("kind")
                ).where(model.enabled.is_(True))
                for model, kind in ((Tracker, "tracker"), (ProductWatch, "watch"))
            ]
        ).subquery()
        plan = case(
            (paid.c.rank == 2, str(Plan.POWER)),
            (paid.c.rank == 1, str(Plan.PRO)),
            else_=str(Plan.FREE),
        )
        quota = case(*[(plan == p, self.policy.for_plan(p).max_trackers) for p in Plan], else_=0)
        ranked = (
            select(
                combined.c.id,
                combined.c.kind,
                quota.label("quota"),
                func.row_number()
                .over(
                    partition_by=combined.c.user_id, order_by=(combined.c.created_at, combined.c.id)
                )
                .label("position"),
            )
            .outerjoin(paid, paid.c.user_id == combined.c.user_id)
            .where(
                or_(
                    combined.c.kind == "tracker",
                    plan.in_([p for p in Plan if self.policy.for_plan(p).comparison_search]),
                )
            )
            .subquery()
        )
        eligible = (
            select(ranked.c.id, ranked.c.kind).where(ranked.c.position <= ranked.c.quota).cte()
        )
        return (
            select(eligible.c.id).where(eligible.c.kind == "tracker"),
            select(eligible.c.id).where(eligible.c.kind == "watch"),
        )

    async def stored_count(self, session: AsyncSession, user_id: UUID) -> int:
        return sum(
            [
                (
                    await session.scalar(
                        select(func.count()).select_from(model).where(model.user_id == user_id)
                    )
                )
                or 0
                for model in (Tracker, ProductWatch)
            ]
        )

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
            total = await self.stored_count(session, user_id)
            active = 0
            for model, ids in (
                (Tracker, self.scheduled_tracker_ids(utcnow())),
                (ProductWatch, self.scheduled_watch_ids(utcnow())),
            ):
                active += (
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(
                            model.user_id == user_id,
                            model.id.in_(ids),
                        )
                    )
                ) or 0
            return total, active
