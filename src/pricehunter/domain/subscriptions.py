from dataclasses import dataclass
from enum import StrEnum


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    POWER = "power"


@dataclass(frozen=True)
class PlanLimits:
    max_trackers: int
    check_interval_seconds: int


class SubscriptionPolicy:
    def __init__(self, limits: dict[Plan, PlanLimits]) -> None:
        self.limits = limits

    def for_plan(self, plan: str) -> PlanLimits:
        return self.limits[Plan(plan)]
