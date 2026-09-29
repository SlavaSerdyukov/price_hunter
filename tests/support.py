from datetime import timedelta

from pricehunter.db.base import utcnow
from pricehunter.db.models import Subscription


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
