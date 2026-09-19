import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pricehunter.domain.errors import InvalidTargetPriceError
from pricehunter.domain.products import Availability


def percentage_change(previous: Decimal, current: Decimal) -> Decimal:
    if previous <= 0 or current <= 0:
        raise ValueError("Prices must be positive")
    return (current - previous) * Decimal(100) / previous


def parse_target(value: str, current: Decimal) -> Decimal:
    value = value.strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,14}(?:\.\d{1,4})?%?", value):
        raise InvalidTargetPriceError()
    try:
        amount = Decimal(value.rstrip("%"))
    except InvalidOperation as exc:
        raise InvalidTargetPriceError() from exc
    if value.endswith("%"):
        if not 0 < amount < 100:
            raise InvalidTargetPriceError()
        amount = (current * (1 - amount / 100)).quantize(Decimal("0.0001"))
    if amount <= 0:
        raise InvalidTargetPriceError()
    return amount


def anomaly_reason(old: Decimal, new: Decimal, old_currency: str, new_currency: str) -> str | None:
    if not new.is_finite() or new <= 0:
        return "invalid_price"
    if old_currency != new_currency:
        return "currency_changed"
    if new < old * Decimal("0.30"):
        return "extreme_drop"
    return None


@dataclass(frozen=True)
class TrackerRules:
    target: Decimal | None = None
    any_drop: bool = True
    on_target: bool = True
    back_in_stock: bool = True
    historical_low: bool = True


def evaluate_rules(
    rules: TrackerRules,
    *,
    previous: Decimal,
    current: Decimal,
    previous_availability: str,
    availability: str,
    historical_minimum: Decimal,
) -> list[str]:
    if availability != Availability.IN_STOCK:
        return []
    events: list[str] = []
    if rules.on_target and rules.target is not None and current <= rules.target:
        if previous > rules.target or previous_availability != Availability.IN_STOCK:
            events.append("target_reached")
    if rules.back_in_stock and previous_availability == Availability.OUT_OF_STOCK:
        events.append("back_in_stock")
    if rules.historical_low and current < historical_minimum:
        events.append("historical_low")
    if rules.any_drop and current < previous:
        events.append("price_drop")
    # One message per observation: the most useful matching rule wins.
    return events[:1]
