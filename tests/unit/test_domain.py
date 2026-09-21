from decimal import Decimal

import pytest
from pydantic import ValidationError

from pricehunter.core.config import Settings
from pricehunter.core.security import validate_url
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    InvalidTargetPriceError,
    UnsupportedStoreError,
)
from pricehunter.domain.pricing import (
    TrackerRules,
    anomaly_reason,
    evaluate_rules,
    parse_target,
    percentage_change,
)
from pricehunter.domain.products import ProductMatcher, ProductOfferData, identity_key
from pricehunter.domain.subscriptions import Plan, PlanLimits, SubscriptionPolicy
from pricehunter.localization.messages import EN, RU, money, tr
from pricehunter.providers.base import OfferReference
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.providers.registry import ProviderRegistry


@pytest.fixture
async def offer():
    return await MockStoreProvider().resolve_url(
        "https://mock.pricehunter.test/products/headphones"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://mock.pricehunter.test/products/headphones",
        "https://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://169.254.169.254/",
        "https://[::1]/",
        "https://localhost/a",
        "https://user:pass@mock.pricehunter.test/a",
        "https://mock.pricehunter.test:8000/a",
        "https://mock.pricehunter.test.evil.com/a",
        "https://evil.com/a",
        "https://mock.pricehunter.test./a",
        "https://mock.pricehunter.test\\@evil.com/a",
        "https://mock.pricehunter.test/\nfoo",
        "https://[invalid/",
        "file:///etc/passwd",
    ],
)
def test_url_security(url):
    with pytest.raises(InvalidProductUrlError):
        validate_url(url, MockStoreProvider.domains)


async def test_registry_and_mock_restart(offer):
    registry = ProviderRegistry([MockStoreProvider()])
    assert registry.resolve_url(offer.url).name == "mock"
    with pytest.raises(UnsupportedStoreError):
        registry.resolve_url("https://unknown.example/item")
    reference = OfferReference(offer.url, offer.external_id, offer.store_slug, 1, {})
    refreshed = await MockStoreProvider().refresh_offer(reference)
    assert refreshed.price == Decimal("90")
    assert (await MockStoreProvider().refresh_offer(reference)) == refreshed


def test_normalization_and_money(offer):
    data = offer.model_dump()
    data["title"] = "  A   product\n "
    assert ProductOfferData(**data).title == "A product"
    for value in (0, -1, "NaN", "Infinity", 8.99):
        with pytest.raises(ValidationError):
            ProductOfferData(**{**data, "price": value})
    assert isinstance(offer.price, Decimal)
    assert percentage_change(Decimal(100), Decimal(80)) == -20


def test_matching_variants_and_global_identifiers(offer):
    a = offer.model_copy(update={"gtin": "4006381333931"})
    b = offer.model_copy(update={"gtin": None, "ean": "4006381333931", "store_slug": "another"})
    assert ProductMatcher().match(a, b).method == "gtin"
    assert identity_key(a) == identity_key(b)
    different = b.model_copy(update={"variant": {"size": "44"}})
    assert ProductMatcher().match(a, different).matched  # Identical GTIN, missing optional size.
    assert (
        not ProductMatcher()
        .match(a.model_copy(update={"variant": {"size": "42"}}), different)
        .matched
    )
    assert identity_key(a) != identity_key(different)
    with pytest.raises(ValidationError):
        ProductOfferData(**{**offer.model_dump(), "gtin": "4006381333932"})


@pytest.mark.parametrize(("value", "expected"), [("10%", "90"), ("79,99", "79.99"), ("100", "100")])
def test_target(value, expected):
    assert parse_target(value, Decimal(100)) == Decimal(expected)


@pytest.mark.parametrize("value", ["0", "-1", "0%", "100%", "NaN", "inf", "1e9", "abc", "1.23456"])
def test_bad_target(value):
    with pytest.raises(InvalidTargetPriceError):
        parse_target(value, Decimal(100))


def test_rules_and_anomaly():
    assert anomaly_reason(Decimal(100), Decimal("NaN"), "EUR", "EUR") == "invalid_price"
    assert anomaly_reason(Decimal(899), Decimal("8.99"), "EUR", "EUR") == "extreme_drop"
    assert anomaly_reason(Decimal(100), Decimal(95), "EUR", "USD") == "currency_changed"
    assert anomaly_reason(Decimal(100), Decimal(95), "EUR", "EUR") is None
    args = dict(
        previous=Decimal(100),
        current=Decimal(80),
        previous_availability="in_stock",
        availability="in_stock",
        historical_minimum=Decimal(100),
    )
    assert evaluate_rules(TrackerRules(target=Decimal(90)), **args) == ["target_reached"]
    assert evaluate_rules(TrackerRules(), **args) == ["historical_low"]
    args["availability"] = "out_of_stock"
    assert evaluate_rules(TrackerRules(), **args) == []


def test_limits_and_localization():
    policy = SubscriptionPolicy({Plan.FREE: PlanLimits(5, 43200)})
    assert policy.for_plan("free").max_trackers == 5
    assert set(EN) == set(RU)
    assert "&lt;script&gt;" in tr("ru", "support", contact="<script>")
    assert "€" in money(Decimal("100.10"), "EUR", "ru")
    with pytest.raises(ValidationError):
        Settings(environment="production", mock_provider_enabled=True)
