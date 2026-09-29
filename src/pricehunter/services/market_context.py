from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from pricehunter.db.models import User
from pricehunter.domain.errors import CountryRequiredError, ProductNotFoundError
from pricehunter.domain.markets import validate_country


async def user_market(session: AsyncSession, user_id: UUID, explicit: str | None = None) -> str:
    user = await session.get(User, user_id)
    if user is None:
        raise ProductNotFoundError()
    country = explicit or user.country_code
    if country is None:
        raise CountryRequiredError()
    return validate_country(country)
