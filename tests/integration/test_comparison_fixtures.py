"""Real adapter payloads through canonical persistence, without retailer network access."""

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from pricehunter.providers.amazon import AmazonCreatorsProvider
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.providers.woocommerce import WooCommerceProvider

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(), parse_float=Decimal)


async def test_ebay_marketplaces_and_normalized_provider_share_gtin(container):
    adapter = EbayBrowseProvider(
        ProviderHTTP(container.http, timeout=5, max_bytes=50000),
        "fixture-id",
        "fixture-secret",
        ["DE", "BE"],
    )
    payload = fixture("ebay_item.json")
    de = adapter._normalize(payload, "DE")
    be_payload = copy.deepcopy(payload)
    be_payload["itemWebUrl"] = "https://www.ebay.be/itm/123456789012"
    be = adapter._normalize(be_payload, "BE")
    # A provider DTO with the same explicit evidence (not a guessed SKU/title match).
    normalized = de.model_copy(
        update={
            "provider": "mock",
            "store_slug": "fixture_merchant",
            "store_name": "Fixture merchant",
            "external_id": "fixture-model",
            "url": "https://mock.pricehunter.test/products/headphones",
        }
    )
    offers = [await container.products.persist(data) for data in (de, be, normalized)]
    assert len({offer.product_id for offer in offers}) == 1
    user = await container.users.telegram(111)
    product = await container.products.product(offers[0].product_id, user.id, market_country="DE")
    assert product.store_count == 2
    assert {o.store_country for o in product.offers} == {"DE"}
    assert product.gtin == "04006381333931"
    used = de.model_copy(update={"external_id": "used", "variant": {"condition": "3000"}})
    assert (await container.products.persist(used)).product_id != product.id


async def test_amazon_scoped_asin_and_variants_persist_from_existing_fixture(container):
    adapter = AmazonCreatorsProvider(
        ProviderHTTP(container.http, timeout=5, max_bytes=50000),
        "fixture-id",
        "fixture-secret",
        {"BE": {"partner_tag": "test-be-21"}, "DE": {"partner_tag": "test-de-21"}},
    )
    payload = fixture("amazon_creators_item.json")
    be = adapter._normalize(payload, "BE")
    payload["detailPageURL"] = "https://www.amazon.de/dp/B012345678?tag=test-de-21"
    de = adapter._normalize(payload, "DE")
    a, b = [await container.products.persist(data) for data in (be, de)]
    assert a.product_id == b.product_id
    payload["asin"] = "B012345679"
    payload["detailPageURL"] = "https://www.amazon.de/dp/B012345679?tag=test-de-21"
    payload["itemInfo"]["productInfo"]["size"]["displayValue"] = "L"
    other = await container.products.persist(adapter._normalize(payload, "DE"))
    assert other.product_id != a.product_id
    user = await container.users.telegram(111)
    comparison = await container.products.product(a.product_id, user.id, market_country="BE")
    assert comparison.store_count == 1 and comparison.variant == {"size": "m", "color": "blue"}


async def test_woocommerce_sku_and_title_never_fabricate_cross_store_identity(container):
    adapter = WooCommerceProvider(
        ProviderHTTP(container.http, timeout=5, max_bytes=50000), "pine64_eu"
    )
    woo = adapter._normalize(fixture("woo_pine64.json"))
    unrelated = (
        MockStoreProvider()
        ._offer("headphones")
        .model_copy(
            update={
                "title": woo.title,
                "sku": woo.sku,
                "brand": woo.brand,
                "model": None,
                "gtin": None,
                "variant": woo.variant,
            }
        )
    )
    first = await container.products.persist(woo)
    second = await container.products.persist(unrelated)
    assert first.product_id != second.product_id
    assert (await container.products.persist(woo)).id == first.id
