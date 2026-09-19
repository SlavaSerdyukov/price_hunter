import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

from redis.asyncio import Redis

from pricehunter.core.config import Settings
from pricehunter.domain.errors import ProviderUnavailableError, RateLimitExceededError

RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""
SLOT_SCRIPT = """
local now = redis.call('TIME')
local seconds = tonumber(now[1]) + tonumber(now[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', seconds)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], seconds + tonumber(ARGV[2]), ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""
UNLOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RateLimiter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def check(self, key: str, *, limit: int, seconds: int = 60) -> None:
        count = await cast(
            Awaitable[int], self.redis.eval(RATE_SCRIPT, 1, f"ph:rate:{key}", str(seconds))
        )
        if int(count) > limit:
            raise RateLimitExceededError()

    async def user(self, user_id: object) -> None:
        await self.check(f"user:{user_id}", limit=self.settings.user_requests_per_minute)

    @asynccontextmanager
    async def offer_refresh(self, offer_id: object) -> AsyncIterator[bool]:
        key, token = f"ph:refresh:{offer_id}", str(uuid4())
        acquired = bool(await self.redis.set(key, token, nx=True, ex=60))
        try:
            yield acquired
        finally:
            if acquired:
                await cast(Awaitable[int], self.redis.eval(UNLOCK_SCRIPT, 1, key, token))

    @asynccontextmanager
    async def provider(self, provider: str) -> AsyncIterator[None]:
        if provider == "mock":
            yield
            return
        limit = self.settings.provider_rate_limits.get(
            provider,
            self.settings.provider_requests_per_minute,
        )
        await self.check(f"provider:{provider}", limit=limit)
        key, token = f"ph:slots:{provider}", str(uuid4())
        claimed = await cast(
            Awaitable[int],
            self.redis.eval(
                SLOT_SCRIPT,
                1,
                key,
                str(self.settings.provider_concurrency),
                "60",
                token,
            ),
        )
        if not claimed:
            raise RateLimitExceededError()
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                yield
        except TimeoutError as exc:
            raise ProviderUnavailableError() from exc
        finally:
            await self.redis.zrem(key, token)
