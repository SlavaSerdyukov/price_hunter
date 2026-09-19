import copy
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProviderUnavailableError,
    UnsupportedProductError,
    VariantSelectionRequiredError,
)
from pricehunter.providers.base import OfferReference
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.woocommerce import WooCommerceProvider, amount


def fixture(name):
    return json.loads((Path(__file__).parents[1] / "fixtures" / name).read_text())


def provider(client, shop="raspberrypi_dk"):
    return WooCommerceProvider(ProviderHTTP(client, timeout=5, max_bytes=10000), shop)


@pytest.mark.parametrize(
    "shop,file,price,currency",
    [
        ("pine64_eu", "woo_pine64.json", "76.75", "EUR"),
        ("raspberrypi_dk", "woo_raspberrypi.json", "699.00", "DKK"),
    ],
)
async def test_real_fixture_resolve_search_refresh(respx_mock, shop, file, price, currency):
    data = fixture(file)
    async with httpx.AsyncClient() as client:
        adapter = provider(client, shop)
        listing = respx_mock.get(adapter.api).respond(200, json=[data])
        refresh = respx_mock.get(f"{adapter.api}/{data['id']}").respond(200, json=data)
        offer = await adapter.resolve_url(data["permalink"])
        assert offer.price == Decimal(price) and offer.currency == currency
        assert "&#" not in offer.title
        assert await adapter.search("display", country="BE", currency=currency) == [offer]
        assert (
            await adapter.refresh_offer(
                OfferReference(
                    offer.url,
                    offer.external_id,
                    offer.store_slug,
                    0,
                    offer.metadata,
                )
            )
            == offer
        )
        assert refresh.call_count == 1
        assert listing.calls[-1].request.url.params["type"] == "simple"


@pytest.mark.parametrize("query", ["attribute_capacity=256GB", "variation_id=348692"])
async def test_exact_variant_and_clean_store_link(respx_mock, query):
    parent, child = fixture("woo_variable.json"), fixture("woo_variation.json")
    child["permalink"] += "&add-to-cart=348678&utm_source=test"
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        respx_mock.get(adapter.api).respond(200, json=[parent])
        respx_mock.get(f"{adapter.api}/{child['id']}").respond(200, json=child)
        offer = await adapter.resolve_url(parent["permalink"] + "?" + query)
        assert offer.external_id == "348692" and offer.price == Decimal("499.00")
        assert "256GB" in offer.title and offer.metadata["parent_id"] == 348678
        assert "variation_id=348692" in offer.url and "add-to-cart" not in offer.url
        assert "utm_source" not in offer.url


@pytest.mark.parametrize(
    "query",
    [
        "",
        "attribute_capacity=",
        "attribute_capacity=512GB",
        "variation_id=1",
        "attribute_unknown=256GB",
    ],
)
async def test_unselected_or_ambiguous_variant_is_not_a_minimum_price(respx_mock, query):
    data = fixture("woo_variable.json")
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        respx_mock.get(adapter.api).respond(200, json=[data])
        with pytest.raises(VariantSelectionRequiredError):
            await adapter.resolve_url(data["permalink"] + "?" + query)


async def test_variant_cannot_belong_to_another_product(respx_mock):
    parent, child = fixture("woo_variable.json"), fixture("woo_variation.json")
    child["parent"] = 999
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        respx_mock.get(adapter.api).respond(200, json=[parent])
        respx_mock.get(f"{adapter.api}/{child['id']}").respond(200, json=child)
        with pytest.raises(ProviderUnavailableError):
            await adapter.resolve_url(parent["permalink"] + "?attribute_capacity=256GB")


@pytest.mark.parametrize(
    "url",
    [
        "http://raspberrypi.dk/produkt/test/",
        "https://127.0.0.1/produkt/test/",
        "https://raspberrypi.dk.evil.test/produkt/test/",
        "https://raspberrypi.dk/wp-admin/",
        "https://raspberrypi.dk/produkt/../../admin/",
        "https://user:pass@raspberrypi.dk/produkt/test/",
    ],
)
async def test_untrusted_url_never_causes_a_request(respx_mock, url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidProductUrlError):
            await provider(client).resolve_url(url)
    assert len(respx_mock.calls) == 0


@pytest.mark.parametrize(
    "value,decimals", [("8.99", 2), (-1, 2), ("-100", 2), ("100", True), ("100", -1), ("100", 9)]
)
def test_minor_units_must_be_explicit_integers(value, decimals):
    with pytest.raises(ValueError):
        amount(value, decimals)


async def test_sale_stock_and_unsupported_types():
    data = fixture("woo_raspberrypi.json")
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        data["prices"]["regular_price"] = "79900"
        result = adapter._normalize(data)
        assert result.original_price == Decimal("799")
        data["is_on_backorder"] = True
        assert adapter._normalize(data).availability == "unknown"
        data["type"] = "grouped"
        with pytest.raises(UnsupportedProductError):
            adapter._normalize(data)


async def test_search_skips_bad_items_and_honors_currency(respx_mock):
    data = fixture("woo_raspberrypi.json")
    bad = copy.deepcopy(data)
    bad["prices"]["price"] = "0"
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        respx_mock.get(adapter.api).respond(200, json=[bad, data])
        assert len(await adapter.search("display")) == 1
        assert await adapter.search("display", currency="EUR") == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "http://169.254.169.254/"}),
        httpx.Response(200, content=b"x" * 10001),
        httpx.Response(200, json={"wrong": "shape"}),
    ],
)
async def test_array_transport_retains_response_bounds(respx_mock, response):
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        respx_mock.get(adapter.api).mock(return_value=response)
        with pytest.raises(ProviderUnavailableError):
            await adapter.search("display")


@pytest.mark.parametrize(
    "query",
    [
        "variation_id=6263",
        "attribute_pa_size=extrasmall&attribute_pa_color=pomelored123",
        "attribute_pa_size=XS&attribute_pa_color=Pomelo+Red",
    ],
)
async def test_clothing_exact_size_and_color_minor_unit_zero(respx_mock, query):
    parent = fixture("woo_hemptees_parent.json")
    child = fixture("woo_hemptees_variant.json")
    async with httpx.AsyncClient() as client:
        adapter = provider(client, "hemptees_be")
        adapter.http.max_bytes = 50000
        respx_mock.get(adapter.api).respond(200, json=[parent])
        refresh = respx_mock.get(f"{adapter.api}/{child['id']}").respond(200, json=child)
        offer = await adapter.resolve_url(parent["permalink"] + "?" + query)
        assert offer.price == Decimal("40") and offer.currency == "EUR"
        assert "XS" in offer.title and "Pomelo Red" in offer.title
        assert offer.external_id == "6263" and offer.metadata == {"parent_id": 5506}
        assert (
            await adapter.refresh_offer(
                OfferReference(
                    offer.url,
                    offer.external_id,
                    offer.store_slug,
                    0,
                    offer.metadata,
                )
            )
            == offer
        )
        assert refresh.call_count == 2


@pytest.mark.parametrize(
    "query", ["", "attribute_pa_color=black", "variation_id=6263&attribute_pa_size=M"]
)
async def test_clothing_requires_selection_and_offers_named_choices(respx_mock, query):
    parent = fixture("woo_hemptees_parent.json")
    async with httpx.AsyncClient() as client:
        adapter = provider(client, "hemptees_be")
        adapter.http.max_bytes = 50000
        respx_mock.get(adapter.api).respond(200, json=[parent])
        with pytest.raises(VariantSelectionRequiredError) as error:
            await adapter.resolve_url(parent["permalink"] + "?" + query)
        assert len(error.value.options) == 35
        assert any(o.label == "Size: XS, Color: Pomelo Red" for o in error.value.options)
        assert all(
            o.url.startswith(parent["permalink"] + "?variation_id=") for o in error.value.options
        )
        assert len(respx_mock.calls) == 1


async def test_clothing_search_returns_specific_variations(respx_mock):
    child = fixture("woo_hemptees_variant.json")
    async with httpx.AsyncClient() as client:
        adapter = provider(client, "hemptees_be")
        respx_mock.get(
            adapter.api, params={"search": "Tee", "type": "simple", "per_page": "5"}
        ).respond(200, json=[])
        respx_mock.get(
            adapter.api, params={"search": "Tee", "type": "variation", "per_page": "5"}
        ).respond(200, json=[child])
        results = await adapter.search("Tee")
        assert len(results) == 1 and results[0].external_id == "6263"
        assert "variation_id=6263" in results[0].url


async def test_westernshop_french_product_url(respx_mock):
    item = fixture("woo_westernshop.json")
    async with httpx.AsyncClient() as client:
        adapter = provider(client, "westernshop_be")
        respx_mock.get(adapter.api).respond(200, json=[item])
        offer = await adapter.resolve_url(item["permalink"])
        assert offer.price == Decimal("49") and offer.country == "BE"
        assert offer.store_slug == "westernshop_be"
