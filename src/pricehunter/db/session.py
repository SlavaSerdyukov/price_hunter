from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pricehunter.core.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_sessions(settings: Settings) -> SessionFactory:
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_recycle=settings.database_pool_recycle_seconds,
        connect_args={"command_timeout": settings.database_command_timeout_seconds},
        hide_parameters=True,
    )
    return async_sessionmaker(engine, expire_on_commit=False)
