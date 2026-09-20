from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pricehunter.domain.errors import FeatureRequiresUpgradeError


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    POWER = "power"


class Feature(StrEnum):
    TARGET_ALERTS = "target_price_alerts"
    HISTORICAL_LOW = "historical_low_alerts"
    BACK_IN_STOCK = "back_in_stock_alerts"
    COMPARISON_SEARCH = "comparison_search"
    HISTORY = "history_access"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"  # Renewal stopped; paid periods still grant access.
    EXPIRED = "expired"
    REFUNDED = "refunded"


@dataclass(frozen=True)
class PlanEntitlements:
    max_trackers: int
    check_interval_seconds: int
    target_price_alerts: bool = False
    historical_low_alerts: bool = False
    back_in_stock_alerts: bool = False
    comparison_search: bool = True
    search_limit: int = 3  # Requests per rolling 24-hour window.
    search_result_limit: int = 3
    history_access: bool = True
    history_days: int = 7

    def allows(self, feature: Feature) -> bool:
        return bool(getattr(self, feature.value))

    def require(self, feature: Feature) -> None:
        if not self.allows(feature):
            raise FeatureRequiresUpgradeError(feature.value)


# Backwards-compatible name for the original policy DTO, not a second policy.
PlanLimits = PlanEntitlements


class SubscriptionPolicy:
    def __init__(self, limits: dict[Plan, PlanEntitlements]) -> None:
        self.limits = limits

    def for_plan(self, plan: str) -> PlanEntitlements:
        return self.limits[Plan(plan)]

    @staticmethod
    def rank(plan: str) -> int:
        return {Plan.FREE: 0, Plan.PRO: 1, Plan.POWER: 2}[Plan(plan)]


@dataclass(frozen=True)
class EffectiveEntitlement:
    plan: Plan
    entitlements: PlanEntitlements
    status: str = "free"
    valid_until: datetime | None = None
    subscription_id: UUID | None = None
    auto_renew: bool | None = None
