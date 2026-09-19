import os
from unittest.mock import AsyncMock

import pytest
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.worker import Worker

from pricehunter.jobs.worker import refresh_due_offers, refresh_offer, send_notifications
from pricehunter.schemas.api import TrackerCreate

pytestmark = pytest.mark.integration


async def test_arq_queue_and_worker(container, monkeypatch):
    redis_url = os.getenv("TEST_REDIS_URL")
    if redis_url:
        if not redis_url.endswith("/15"):
            raise RuntimeError("TEST_REDIS_URL must use dedicated database 15")
        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        await pool.flushdb()
    else:
        pool = ArqRedis(connection_pool=container.redis.connection_pool)
        # fakeredis does not implement INFO; only the startup log is replaced.
        # CI uses a real Redis service and exercises this call too.
        monkeypatch.setattr("arq.worker.log_redis_info", AsyncMock())
    user = await container.users.telegram(123)
    offer = await container.products.resolve(
        "https://mock.pricehunter.test/products/headphones", user.id
    )
    await container.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
    ctx = {"redis": pool, "container": container}
    assert await refresh_due_offers(ctx) == 1
    assert await refresh_due_offers(ctx) == 0
    assert await send_notifications(ctx) == 0
    worker = Worker(
        functions=[refresh_offer],
        redis_pool=pool,
        burst=True,
        handle_signals=False,
        poll_delay=0.01,
        ctx={"container": container},
    )
    try:
        assert await worker.run_check() == 1
        assert (await container.products.offer(offer.id)).price == 95
    finally:
        await worker.close()
