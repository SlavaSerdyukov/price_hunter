import os
from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.db.base import Base
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def sessions():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated migrated PostgreSQL test database")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL must end in a database name with suffix _test")
    engine = create_async_engine(url, hide_parameters=True)
    async with engine.begin() as connection:
        await connection.execute(text("SELECT version_num FROM alembic_version"))
        names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
        await connection.execute(text(f"TRUNCATE {names} CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def container(sessions, redis) -> AsyncIterator[Container]:
    settings = Settings(
        _env_file=None,
        environment="test",
        provider_data_policies={
            name: SYNTHETIC_POLICY
            for name in (
                "ebay",
                "amazon",
                "woocommerce_pine64_eu",
                "woocommerce_hemptees_be",
                "woocommerce_westernshop_be",
                "woocommerce_raspberrypi_dk",
                "failed",
                "empty",
                "summaries",
            )
        },
        user_requests_per_minute=100,
        notification_cooldown_seconds=0,
        telegram_bot_token=SecretStr("123456789:TEST_TOKEN_FOR_LOCAL_TESTS_ONLY_12345"),
    )
    resources = Container(settings, sessions=sessions, redis=redis)
    yield resources
    await resources.close()
