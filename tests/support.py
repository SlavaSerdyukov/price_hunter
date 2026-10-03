from datetime import datetime, timedelta

from pydantic import BaseModel

from pricehunter.db.base import utcnow
from pricehunter.db.models import Subscription


def comparison_semantics(value, *, started_at, finished_at, previous_ages):
    """Keep stable comparison fields strict; validate request-time ages separately."""

    def stable(value):
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, list):
            return [stable(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "age_seconds" in value:
            age = value["age_seconds"]
            checked = datetime.fromisoformat(value["last_checked_at"])
            assert isinstance(age, int) and age >= 0
            assert max(0, int((started_at - checked).total_seconds())) <= age
            assert age <= max(0, int((finished_at - checked).total_seconds()))
            assert age >= previous_ages.get(value["offer_id"], 0)
            previous_ages[value["offer_id"]] = age
        return {key: stable(item) for key, item in value.items() if key != "age_seconds"}

    return stable(value)


async def grant_plan(container, user_id, plan="pro", *, until=None):
    """Seed a migrated legacy subscription for pre-billing tracking regression tests."""
    async with container.sessions.begin() as session:
        row = Subscription(
            user_id=user_id,
            plan=plan,
            provider="legacy_test",
            status="active",
            valid_from=utcnow() - timedelta(days=1),
            valid_until=until or utcnow() + timedelta(days=30),
            auto_renew=False,
        )
        session.add(row)
        await session.flush()
        return row.id


async def market_user(container, telegram_id, country="DE"):
    from pricehunter.schemas.api import UserSettingsPatch

    user = await container.users.telegram(telegram_id)
    return await container.users.settings(user.id, UserSettingsPatch(country_code=country))
