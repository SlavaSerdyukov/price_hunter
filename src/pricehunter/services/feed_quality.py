"""Constant-memory feed metrics and shared structural/publication gates."""

import hashlib
import json
from decimal import Decimal
from typing import Any

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.db.models import MerchantProgram
from pricehunter.domain.errors import PriceHunterError
from pricehunter.domain.feeds import FeedError, FeedProductData, FeedReport, RejectedFeedRow
from pricehunter.providers.feeds.normalization import validate_links


def configuration_fingerprint(program: MerchantProgram) -> str:
    fields = (
        "network",
        "external_merchant_id",
        "external_feed_id",
        "market_country",
        "currency",
        "feed_mode",
        "feed_language",
        "domain",
    )
    raw = json.dumps(
        {key: getattr(program, key) for key in fields}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def version_digest(value: str | None) -> str | None:
    # Upstream versions are opaque. Never persist possible URLs or credential text.
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


class FeedQualityEvaluator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def count(
        self, report: FeedReport, item: FeedProductData | RejectedFeedRow, program: MerchantProgram
    ) -> None:
        report.rows_parsed += 1
        if report.rows_parsed > self.settings.feed_max_rows:
            raise FeedError("row_limit")
        if isinstance(item, RejectedFeedRow):
            report.invalid_rows += 1
            # Fixed category set; never persist arbitrary adapter text or row URLs.
            code = (
                item.code
                if item.code
                in {
                    "invalid_product",
                    "invalid_gtin",
                    "invalid_money",
                    "invalid_row",
                    "invalid_link",
                }
                else "invalid_row"
            )
            report.rejections[code] = report.rejections.get(code, 0) + 1
            return
        if item.currency != program.currency:
            raise FeedError("wrong_currency")
        if item.source_updated_at and item.source_updated_at > utcnow():
            raise FeedError("future_source_version")
        try:
            validate_links(item, program)
        except PriceHunterError:
            raise FeedError("invalid_destination_host") from None
        report.valid_rows += 1
        report.identifier_coverage += bool(item.gtin or item.ean or item.upc or item.mpn)
        report.variant_coverage += bool(item.variant)
        report.availability_coverage += item.availability != "unknown"
        report.currencies[item.currency] = report.currencies.get(item.currency, 0) + 1
        signals = {
            "gtin": bool(item.gtin or item.ean or item.upc),
            "mpn": bool(item.mpn),
            "model": bool(item.model),
            "affiliate_link": bool(item.affiliate_url),
            "direct_link": bool(item.direct_url),
            "destination_host": True,
            "image": bool(item.image_url),
            "size": bool(item.variant.get("size")),
            "colour": bool(item.variant.get("colour") or item.variant.get("color")),
            "capacity": bool(item.variant.get("capacity")),
            "parent": bool(item.parent_external_id),
            "source_timestamp": item.source_updated_at is not None,
            "delivery_cost": item.delivery_cost is not None,
            "original_price": item.original_price is not None,
            "in_stock": item.availability == "in_stock",
            "out_of_stock": item.availability == "out_of_stock",
            "unknown": item.availability == "unknown",
        }
        for key, present in signals.items():
            report.coverage[key] = report.coverage.get(key, 0) + int(present)
        report.price_min = (
            item.price if report.price_min is None else min(report.price_min, item.price)
        )
        report.price_max = (
            item.price if report.price_max is None else max(report.price_max, item.price)
        )

    def evaluate(
        self,
        report: FeedReport,
        *,
        previous_rows: int = 0,
        previous_report: dict[str, Any] | None = None,
        allow_shrink: bool = False,
    ) -> bool:
        """Return whether shrink was overridden; hard gates always apply first."""
        report.completed_at = utcnow()
        report.valid_ratio = Decimal(report.valid_rows) / max(1, report.rows_parsed)
        unique = report.valid_rows - report.duplicates
        old = previous_report or {}
        report.drift = {"row_count_delta": unique - previous_rows}
        if old:
            report.drift["valid_ratio_delta"] = str(
                report.valid_ratio - Decimal(str(old.get("valid_ratio", 1)))
            )
            old_valid = max(1, int(old.get("valid_rows", 0)))
            report.drift["coverage_delta"] = {
                key: str(
                    Decimal(count) / max(1, report.valid_rows)
                    - Decimal(old.get("coverage", {}).get(key, 0)) / old_valid
                )
                for key, count in report.coverage.items()
            }
            report.drift["currency_distribution_changed"] = report.currencies != old.get(
                "currencies", {}
            )
            if (
                any(Decimal(v) != 0 for v in report.drift["coverage_delta"].values())
                or report.drift["row_count_delta"]
            ):
                report.warnings.append("review_generation_drift")
        if unique < self.settings.feed_min_valid_rows:
            self._reject(report, "quality_min_rows")
        if report.invalid_rows > self.settings.feed_max_invalid_ratio * report.rows_parsed:
            self._reject(report, "quality_invalid_ratio")
        shrink = (
            previous_rows >= self.settings.feed_shrink_guard_min_previous_rows
            and Decimal(previous_rows - unique)
            > self.settings.feed_max_shrink_ratio * previous_rows
        )
        if shrink and not allow_shrink:
            self._reject(report, "quality_shrink")
        if shrink:
            report.warnings.append("manual_override_used")
        return shrink

    @staticmethod
    def _reject(report: FeedReport, code: str) -> None:
        report.quality_error, report.failure_kind = code, "publication_quality_failed"
        raise FeedError(code)
