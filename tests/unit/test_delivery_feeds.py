from decimal import Decimal

import pytest

from pricehunter.domain.feeds import FeedProductData
from pricehunter.providers.base import StoreProvider
from pricehunter.providers.feeds.cj import CJFeedSource
from tests.integration.test_commerce_feeds import row
from tests.unit.test_cj_adapter import fixture
from tests.unit.test_feed_adapters import program


@pytest.mark.parametrize("cost", [None, Decimal("0"), Decimal("4.99")])
def test_unscoped_feed_amount_is_first_class_but_never_authorizes_delivery(cost):
    data = row(delivery_cost=cost)
    offer = data.offer(
        program_id=None,
        network="awin",
        merchant_id="1",
        name="Fixture",
        domain="example.com",
        market="BE",
    )
    assert offer.shipping_price == cost
    assert offer.delivery_scope == offer.tax_status == "unknown"
    assert offer.delivery_country is None and offer.delivery_values()["delivery_total"] is None
    assert "delivery_cost" not in offer.metadata
    assert FeedProductData.model_validate_json(data.model_dump_json()) == data


@pytest.mark.parametrize("amount", ["0", "4.99"])
def test_cj_documented_country_cost_is_normalized_without_inventing_tax(amount):
    raw = fixture("electronics") | {
        "shipping": {"country": "BE", "price": {"amount": amount, "currency": "EUR"}}
    }
    data = CJFeedSource.normalize(raw, program("cj"))
    offer = data.offer(
        program_id=None,
        network="cj",
        merchant_id="101",
        name="Fixture",
        domain="example.com",
        market="BE",
    )
    assert offer.shipping_price == Decimal(amount)
    assert offer.delivery_country == "BE" and offer.delivery_scope == "country"
    assert offer.tax_status == "unknown" and offer.delivery_values()["delivery_total"] is None


@pytest.mark.parametrize(
    "shipping",
    [
        None,
        {"country": "BE", "price": {"amount": "0", "currency": "GBP"}},
        {"country": "NL", "price": {"amount": "0", "currency": "EUR"}},
        {"country": "BE", "postalCode": "2000", "price": {"amount": "0", "currency": "EUR"}},
    ],
)
def test_cj_missing_wrong_currency_or_destination_cost_stays_unknown(shipping):
    data = CJFeedSource.normalize(fixture("electronics") | {"shipping": shipping}, program("cj"))
    assert data.delivery_cost is None and data.delivery_scope == "unknown"


def test_generic_product_endpoint_does_not_claim_dynamic_quote_capability():
    from pricehunter.domain.discovery import Capability

    assert Capability.DELIVERY_QUOTE not in StoreProvider.capabilities
