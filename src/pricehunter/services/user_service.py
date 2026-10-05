import secrets
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from pricehunter.db.base import utcnow
from pricehunter.db.models import APIKey, User
from pricehunter.db.session import SessionFactory
from pricehunter.domain.errors import InvalidDeliveryContextError, ProductNotFoundError
from pricehunter.localization.languages import DEFAULT_LANGUAGE, normalize_language
from pricehunter.schemas.api import UserSettingsPatch


class UserService:
    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    async def telegram(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        language: str | None = None,
        referral: str | None = None,
    ) -> User:
        async with self.sessions.begin() as session:
            referrer = None
            if referral:
                referrer = await session.scalar(
                    select(User.id).where(
                        User.referral_code == referral,
                        User.telegram_user_id != telegram_id,
                    )
                )
            statement = (
                insert(User)
                .values(
                    id=uuid4(),
                    telegram_user_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    language_code=normalize_language(language or DEFAULT_LANGUAGE),
                    referred_by=referrer,
                    referral_code=secrets.token_urlsafe(12),
                )
                .on_conflict_do_update(
                    index_elements=[User.telegram_user_id],
                    set_={
                        "username": username,
                        "first_name": first_name,
                        "last_active_at": utcnow(),
                    },
                )
                .returning(User)
            )
            return (await session.scalars(statement)).one()

    async def language(self, telegram_id: int, *, fallback: str | None = None) -> str:
        """Read the chosen language without creating a user for financial updates."""
        async with self.sessions() as session:
            language = await session.scalar(
                select(User.language_code).where(User.telegram_user_id == telegram_id)
            )
            return normalize_language(language or fallback or DEFAULT_LANGUAGE)

    async def authenticate(self, digest: str) -> User | None:
        async with self.sessions() as session:
            return (
                await session.scalars(
                    select(User)
                    .join(APIKey)
                    .where(
                        APIKey.digest == digest,
                        APIKey.revoked_at.is_(None),
                    )
                )
            ).one_or_none()

    async def settings(self, user_id: UUID, patch: UserSettingsPatch) -> User:
        async with self.sessions.begin() as session:
            user = await session.get(User, user_id, with_for_update=True)
            if user is None:
                raise ProductNotFoundError()
            values = patch.model_dump(exclude_unset=True)
            if "delivery_country" in values and values["delivery_country"] != user.delivery_country:
                # Never carry an old country's postal preference into a new country.
                if "delivery_postal_code" not in values:
                    values["delivery_postal_code"] = None
            country = values.get("delivery_country", user.delivery_country)
            postal = values.get("delivery_postal_code", user.delivery_postal_code)
            if postal is not None and country is None:
                raise InvalidDeliveryContextError()
            for field, value in values.items():
                setattr(user, field, value)
            return user
