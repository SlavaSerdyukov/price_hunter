from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pricehunter.core.security import validate_url
from pricehunter.db.models import MerchantProgram
from pricehunter.domain.feeds import AFFILIATE_HOSTS, FeedProductData
from pricehunter.domain.products import Availability


def timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1000, UTC)
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result if result.tzinfo else None


def stock(value: Any, quantity: Any = None) -> Availability:
    if quantity not in (None, ""):
        return Availability.IN_STOCK if Decimal(str(quantity)) > 0 else Availability.OUT_OF_STOCK
    normalized = str(value or "").strip().casefold().replace("_", " ")
    if normalized in {"1", "true", "in stock", "available"}:
        return Availability.IN_STOCK
    if normalized in {"0", "false", "out of stock", "unavailable"}:
        return Availability.OUT_OF_STOCK
    return Availability.UNKNOWN


def validate_links(item: FeedProductData, program: MerchantProgram) -> FeedProductData:
    validate_url(item.affiliate_url, AFFILIATE_HOSTS[program.network])
    if item.direct_url:
        validate_url(
            item.direct_url, {program.domain, "www." + program.domain.removeprefix("www.")}
        )
    return item
