from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from pricehunter.domain.comparison import (
    ComparisonOffer,
    ComparisonProduct,
    comparison_rank,
    currency_comparisons,
)
from pricehunter.domain.products import ProductMatcher, identity_key, model_code, normalized_variant
from pricehunter.providers.mock import MockStoreProvider


@pytest.mark.parametrize(
    "value", ["WH-1000XM6", "WH 1000 XM6", "ｗｈ－１０００ｘｍ６", "wh_1000.xm6"]
)
def test_model_normalization(value):
    assert model_code(value) == "wh1000xm6"
    assert model_code("A+1") != model_code("A1")
    assert model_code("128GB") != model_code("256GB")


def test_variant_aliases_preserve_size_capacity_and_unknown_attributes():
    assert normalized_variant({"Couleur": "Noir", "Storage": "128 GB"}) == {
        "color": "black",
        "capacity": "128gb",
    }
    assert normalized_variant({"Größe": "42,5"}) == {"size": "42.5"}
    assert normalized_variant({"size": "42"}) != normalized_variant({"size": "44"})
    assert normalized_variant({"size": "42", "talla": "44"}) != {"size": "44"}
    assert normalized_variant({"UK size": "8"}) != normalized_variant({"EU size": "8"})


@pytest.mark.parametrize(
    "change,matched,method",
    [
        ({}, True, "gtin"),
        ({"gtin": None, "ean": "4006381333931"}, True, "gtin"),
        ({"gtin": "5901234123457"}, False, "identifier_conflict"),
        ({"ean": "5901234123457"}, False, "identifier_conflict"),
        ({"model": "WH1000XM7"}, False, "identifier_conflict"),
        ({"brand": "Other"}, False, "identifier_conflict"),
        ({"variant": {"color": "white"}}, False, "variant_mismatch"),
        ({"variant": {}}, True, "gtin"),
        ({"variant": {"Farbe": "Schwarz"}}, True, "gtin"),
        ({"gtin": None}, True, "brand_model"),
        ({"gtin": None, "mpn": "WH1000XM6"}, True, "manufacturer_model"),
    ],
)
def test_match_diagnostics(change, matched, method):
    provider = MockStoreProvider()
    a = provider._offer("sony-a")
    b = provider._offer("sony-b").model_copy(update=change)
    result = ProductMatcher().match(a, b)
    assert result.matched is matched and result.method == method
    assert result.reasons
    if matched:
        assert result.confidence >= Decimal("0.95")


def test_weak_titles_and_conflicting_variants_do_not_merge():
    a = MockStoreProvider()._offer("model-a")
    b = MockStoreProvider()._offer("model-b")
    assert ProductMatcher().match(a, b).matched and identity_key(a) == identity_key(b)
    assert not ProductMatcher().match(a, MockStoreProvider()._offer("model-256")).matched
    weak_a = a.model_copy(update={"brand": None, "model": None})
    weak_b = b.model_copy(update={"brand": None, "model": None})
    assert ProductMatcher().match(weak_a, weak_b).method == "insufficient_evidence"
    assert ProductMatcher().match(weak_a, weak_a).method == "listing_identity"
    for title_a, title_b in (("Phone 128 GB", "Phone 256GB"), ("Shoe size 42", "Shoe size 44")):
        assert (
            not ProductMatcher()
            .match(a.model_copy(update={"title": title_a}), b.model_copy(update={"title": title_b}))
            .matched
        )
    assert identity_key(a) == identity_key(
        a.model_copy(update={"title": "New merchant description"})
    )


def test_asin_scope_and_conflicts():
    a = (
        MockStoreProvider()
        ._offer("model-a")
        .model_copy(
            update={"provider": "amazon", "brand": None, "model": None, "asin": "B000000001"}
        )
    )
    b = a.model_copy(update={"store_slug": "amazon_de", "external_id": "other"})
    assert ProductMatcher().match(a, b).method == "asin"
    assert not ProductMatcher().match(a, b.model_copy(update={"asin": "B000000002"})).matched
    assert not ProductMatcher().match(a, b.model_copy(update={"provider": "woocommerce"})).matched


def comparison_offer(price, availability="in_stock", currency="EUR", store="Demo"):
    return ComparisonOffer(
        offer_id=uuid4(),
        store=store,
        store_slug=store,
        store_country="DE",
        title="Sony",
        price=Decimal(price),
        currency=currency,
        availability=availability,
        url="https://example.com/product",
        image_url=None,
        last_checked_at=datetime.now(UTC),
    )


def test_currency_and_availability_policy():
    out = comparison_offer("100", "out_of_stock")
    unknown = comparison_offer("90", "unknown")
    available = comparison_offer("110")
    expensive = comparison_offer("130")
    usd = comparison_offer("1", currency="USD")
    groups = currency_comparisons([out, available, expensive, unknown, usd])
    assert [g.currency for g in groups] == ["EUR", "USD"]
    assert groups[0].best_available_offer == available
    assert groups[0].cheapest_known_offer == unknown
    assert groups[0].price_spread == 20
    assert groups[1].best_available_offer == usd
    none = currency_comparisons([out, unknown])[0]
    assert none.best_available_offer is None and none.price_spread is None
    assert available.total_price is None and available.shipping_price is None


def test_ranking_is_deterministic_and_ignores_cross_currency_numbers():
    offer = comparison_offer("329")
    base = ComparisonProduct(
        id=uuid4(),
        canonical_name="Sony WH-1000XM6",
        brand="Sony",
        model="WH-1000XM6",
        variant={},
        image_url=None,
        offers=[offer],
        currencies=["EUR"],
        currency_groups=currency_comparisons([offer]),
        best_available_offer=offer,
        price_spread=Decimal(0),
        match_confidence=Decimal(1),
        store_count=2,
        offer_count=2,
    )
    low = base.model_copy(
        update={"id": uuid4(), "canonical_name": "Headphones", "model": None, "store_count": 1}
    )
    assert sorted([low, base], key=lambda p: comparison_rank(p, "Sony WH1000XM6")) == [base, low]
    assert sorted([base, low], key=lambda p: comparison_rank(p, "Sony WH1000XM6")) == [base, low]
