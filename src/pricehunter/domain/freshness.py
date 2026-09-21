from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True)
class OfferFreshnessPolicy:
    seconds: dict[str, int]

    def ttl(self, provider: str) -> int:
        return self.seconds.get(
            provider, self.seconds.get(provider.split("_", 1)[0], self.seconds["default"])
        )

    def classify(
        self,
        provider: str,
        checked_at: datetime,
        now: datetime,
        *,
        failures: int = 0,
        quarantined: bool = False,
    ) -> Freshness:
        age = (now - checked_at).total_seconds()
        if failures or quarantined or age < -60:
            return Freshness.FAILED
        return (
            Freshness.FRESH
            if self.ttl(provider) > 0 and age < self.ttl(provider)
            else Freshness.STALE
        )
