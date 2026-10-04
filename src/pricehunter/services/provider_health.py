from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

from pricehunter.db.base import utcnow
from pricehunter.domain.search import ProviderSearchOutcome

HEALTH_SCRIPT = """
if ARGV[1] == 'NETWORK_FAILED' then
    redis.call('HSET', KEYS[1], 'last_failure', ARGV[2], 'error_code', 'provider_failed')
    redis.call('HINCRBY', KEYS[1], 'consecutive_failures', 1)
else
    redis.call('HSET', KEYS[1], 'last_success', ARGV[2], 'consecutive_failures', 0)
end
redis.call('HSET', KEYS[1], 'status', ARGV[1])
redis.call('EXPIRE', KEYS[1], 604800)
return 1
"""


class ProviderHealth:
    """Expiring aggregate search health; no users, queries or raw remote failures."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def record(self, outcome: ProviderSearchOutcome) -> None:
        await cast(
            Awaitable[int],
            self.redis.eval(
                HEALTH_SCRIPT,
                1,
                "ph:search-health:" + outcome.provider,
                outcome.status,
                utcnow().isoformat(),
            ),
        )

    async def read(self, provider: str) -> dict[str, Any]:
        data = await cast(
            Awaitable[dict[bytes, bytes]], self.redis.hgetall("ph:search-health:" + provider)
        )
        return {k.decode(): v.decode() for k, v in data.items()}
