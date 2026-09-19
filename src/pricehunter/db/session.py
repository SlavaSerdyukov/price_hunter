from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pricehunter.core.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_sessions(settings: Settings) -> SessionFactory:
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        hide_parameters=True,
    )
    return async_sessionmaker(engine, expire_on_commit=False)
