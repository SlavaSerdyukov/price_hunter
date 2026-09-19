import asyncio

import pytest

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.domain.errors import ProviderUnavailableError, RateLimitExceededError


async def test_rate_limit_is_shared_and_expires(redis):
    limiter = RateLimiter(redis, Settings(_env_file=None))
    await limiter.check("test", limit=2)
    await limiter.check("test", limit=2)
    with pytest.raises(RateLimitExceededError):
        await RateLimiter(redis, Settings(_env_file=None)).check("test", limit=2)
    assert 0 < await redis.ttl("ph:rate:test") <= 60


async def test_provider_slots_release_after_failure(redis):
    limiter = RateLimiter(redis, Settings(_env_file=None, provider_concurrency=1))
    with pytest.raises(RuntimeError):
        async with limiter.provider("ebay"):
            with pytest.raises(RateLimitExceededError):
                async with limiter.provider("ebay"):
                    pytest.fail("Should never enter a second provider slot")
            raise RuntimeError("provider failed")
    async with limiter.provider("ebay"):
        assert await redis.zcard("ph:slots:ebay") == 1
    assert await redis.zcard("ph:slots:ebay") == 0


async def test_shop_specific_limit_is_shared_across_instances(redis):
    settings = Settings(_env_file=None, provider_rate_limits={"woocommerce_pine64_eu": 1})
    async with RateLimiter(redis, settings).provider("woocommerce_pine64_eu"):
        pass
    with pytest.raises(RateLimitExceededError):
        async with RateLimiter(redis, settings).provider("woocommerce_pine64_eu"):
            pytest.fail("The per-shop limit must apply across workers")
    async with RateLimiter(redis, settings).provider("ebay"):
        pass


async def test_operation_deadline_is_provider_unavailable_and_releases_slot(redis):
    limiter = RateLimiter(redis, Settings(_env_file=None, provider_timeout_seconds=1))
    with pytest.raises(ProviderUnavailableError):
        async with limiter.provider("woocommerce_hemptees_be"):
            await asyncio.Event().wait()
    assert await redis.zcard("ph:slots:woocommerce_hemptees_be") == 0
    async with limiter.provider("woocommerce_hemptees_be"):
        pass
