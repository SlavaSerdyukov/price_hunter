from datetime import timedelta

import pytest
from pydantic import ValidationError

from pricehunter.core.config import Settings
from pricehunter.db.base import utcnow
from pricehunter.domain.discovery import Capability as C
from pricehunter.domain.discovery import ProductSearchIdentity
from pricehunter.domain.freshness import Freshness, OfferFreshnessPolicy
from pricehunter.domain.products import ProductMatcher, variant_evidence
from pricehunter.providers.mock import MockStoreProvider


@pytest.mark.parametrize(
    "age,failures,quarantined,expected",
    [
        (0, 0, False, "fresh"),
        (299, 0, False, "fresh"),
        (300, 0, False, "stale"),
        (301, 0, False, "stale"),
        (0, 1, False, "failed"),
        (0, 0, True, "failed"),
        (-61, 0, False, "failed"),
        (-60, 0, False, "fresh"),
    ],
)
def test_freshness_boundaries(age, failures, quarantined, expected):
    now = utcnow()
    policy = OfferFreshnessPolicy({"default": 300})
    assert (
        policy.classify(
            "shop", now - timedelta(seconds=age), now, failures=failures, quarantined=quarantined
        )
        == expected
    )


def test_provider_exact_category_default_and_disabled_policy():
    policy = OfferFreshnessPolicy(
        {"default": 300, "woocommerce": 500, "woocommerce_be": 900, "amazon": 0}
    )
    assert [
        policy.ttl(p)
        for p in ("other", "woocommerce", "woocommerce_be", "woocommerce_fr", "woocommerce_be_more")
    ] == [300, 500, 900, 500, 500]
    assert policy.classify("amazon", utcnow(), utcnow()) == Freshness.STALE


@pytest.mark.parametrize(
    "changes",
    [
        {"offer_freshness_seconds": {"mock": 10}},
        {"offer_freshness_seconds": {"default": -1}},
        {"discovery_plan_seconds": {"free": 60}},
        {"discovery_plan_seconds": {"free": 60, "pro": 60, "power": 0}},
        {"discovery_lease_seconds": 1},
    ],
)
def test_invalid_discovery_configuration(changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **changes)


@pytest.mark.parametrize(
    "caps,method,text",
    [
        ({C.SEARCH_GTIN, C.SEARCH_MODEL}, "gtin", "4006381333931"),
        ({C.SEARCH_MODEL}, "mpn", "Sony WH-MPN"),
        ({C.SEARCH_ASIN}, "asin", "B000000001"),
        ({C.SEARCH_KEYWORD}, None, None),
        ({C.REFRESH}, None, None),
    ],
)
def test_discovery_uses_identifiers_and_capabilities(caps, method, text):
    identity = ProductSearchIdentity("4006381333931", "Sony", "WH1000XM6", "WH-MPN", "B000000001")
    query = identity.query(frozenset(caps), "BE", "EUR")
    if method is None:
        assert query is None
    else:
        assert (query.method, query.text, query.country, query.currency) == (
            method,
            text,
            "BE",
            "EUR",
        )
    model = ProductSearchIdentity(None, "Sony", "WH1000XM6", None, None)
    assert model.query(frozenset({C.SEARCH_MODEL}), "DE", "EUR").method == "model"
    assert (
        ProductSearchIdentity(None, None, None, None, None).query(frozenset(caps), "BE", "EUR")
        is None
    )


@pytest.mark.parametrize(
    "key,left,right,gtin,matched,evidence",
    [
        ("color", "black", "black", True, True, "compatible"),
        ("color", "black", None, True, True, "unknown"),
        ("color", None, "black", True, True, "unknown"),
        ("capacity", "128GB", None, True, True, "unknown"),
        ("size", None, "42", True, True, "unknown"),
        ("color", "black", "white", True, False, "conflicting"),
        ("capacity", "128GB", "256GB", True, False, "conflicting"),
        ("size", "42", "44", False, False, "conflicting"),
        ("color", "black", None, False, False, "unknown"),
        ("condition", "new", None, True, False, "unknown"),
        ("variation_id", "1", None, True, False, "unknown"),
    ],
)
def test_missing_attributes_never_weaken_explicit_conflicts(
    key, left, right, gtin, matched, evidence
):
    a, b = [MockStoreProvider()._offer(s) for s in ("sony-a", "sony-b")]
    av, bv = ({key: left} if left else {}), ({key: right} if right else {})
    a = a.model_copy(update={"variant": av, "gtin": a.gtin if gtin else None})
    b = b.model_copy(update={"variant": bv, "gtin": b.gtin if gtin else None})
    assert variant_evidence(av, bv) == evidence
    assert ProductMatcher().match(a, b).matched is matched
