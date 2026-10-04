from decimal import Decimal

import pytest

from pricehunter.core.config import Settings
from pricehunter.domain.feeds import FeedError, FeedReport, RejectedFeedRow
from pricehunter.services.feed_quality import FeedQualityEvaluator, configuration_fingerprint
from tests.integration.test_commerce_feeds import row
from tests.unit.test_feed_adapters import program


@pytest.mark.parametrize("gtin_count,size_count", [(5, 95), (90, 0)])
def test_soft_vertical_quality_does_not_block_structurally_valid_feed(gtin_count, size_count):
    p = program()
    evaluator = FeedQualityEvaluator(Settings(_env_file=None))
    report = FeedReport()
    for n in range(100):
        evaluator.count(
            report,
            row(
                str(n),
                gtin="4548736162563" if n < gtin_count else None,
                variant={"size": "M"} if n < size_count else {},
            ),
            p,
        )
    assert not evaluator.evaluate(report)
    assert report.coverage["gtin"] == gtin_count and report.coverage["size"] == size_count
    assert report.price_min == report.price_max == Decimal("329")


@pytest.mark.parametrize(
    "field,value",
    [
        ("external_feed_id", "502"),
        ("external_merchant_id", "102"),
        ("market_country", "DE"),
        ("currency", "USD"),
        ("network", "cj"),
        ("domain", "other.com"),
        ("feed_language", "fr"),
        ("feed_mode", "incremental"),
    ],
)
def test_all_technical_identity_fields_change_fingerprint(field, value):
    p = program()
    p.feed_mode = "full"
    before = configuration_fingerprint(p)
    setattr(p, field, value)
    assert before != configuration_fingerprint(p)


def test_cosmetics_and_version_are_not_technical_identity():
    p = program()
    before = configuration_fingerprint(p)
    p.display_name, p.version = "Renamed", 100
    assert before == configuration_fingerprint(p)


@pytest.mark.parametrize("rows,error", [(0, "quality_min_rows"), (1, "quality_invalid_ratio")])
def test_first_generation_hard_gates_cannot_be_overridden(rows, error):
    evaluator = FeedQualityEvaluator(Settings(_env_file=None))
    report = FeedReport(rows_parsed=rows + 10, valid_rows=rows, invalid_rows=10)
    with pytest.raises(FeedError, match=error):
        evaluator.evaluate(report, allow_shrink=True)


def test_invalid_ratio_uses_decimal_inclusive_boundary_and_unique_shrink():
    evaluator = FeedQualityEvaluator(Settings(_env_file=None, feed_max_invalid_ratio="0.1"))
    assert not evaluator.evaluate(FeedReport(rows_parsed=10, valid_rows=9, invalid_rows=1))
    with pytest.raises(FeedError, match="quality_shrink"):
        evaluator.evaluate(
            FeedReport(rows_parsed=10000, valid_rows=10000, duplicates=9999), previous_rows=10000
        )


def test_bounded_unknown_error_code_cannot_store_credential_text():
    assert FeedError("https://PRIVATE/?key=secret").code == "invalid_feed"


def test_wrong_currency_future_time_and_destination_are_hard():
    from datetime import timedelta

    from pricehunter.db.base import utcnow

    evaluator = FeedQualityEvaluator(Settings(_env_file=None))
    for data, code in (
        (row(currency="USD"), "wrong_currency"),
        (row(source_updated_at=utcnow() + timedelta(days=1)), "future_source_version"),
        (row(direct_url="https://other.com/product"), "invalid_destination_host"),
    ):
        with pytest.raises(FeedError, match=code):
            evaluator.count(FeedReport(), data, program())
    evaluator = FeedQualityEvaluator(Settings(_env_file=None, feed_max_rows=1))
    report = FeedReport()
    evaluator.count(report, RejectedFeedRow("invalid_product"), program())
    with pytest.raises(FeedError, match="row_limit"):
        evaluator.count(report, row(), program())
